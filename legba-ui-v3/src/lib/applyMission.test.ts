/**
 * `applyMission` against the REAL stores — because the whole claim of a mission
 * is that one act moves four things that used to move separately, and a test
 * against stand-ins would prove only that the stand-ins were called.
 *
 * What is pinned:
 *  - the scope is written before anything else (so the Consult tile the mission
 *    opens comes up already pinned, rather than a frame later);
 *  - the temporal window is the map's AND the scrubber's — one `spanMs` write;
 *  - layers are set WHOLESALE, and a mission with no layer claim leaves the
 *    reader's own selection exactly as it was;
 *  - the mission's tiles go through the caller's opener, and one the opener
 *    declines (mode gating) is simply not reported as opened;
 *  - `skipScope` — the boot path where the link's own `#scope=` wins.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { applyMission } from '@/lib/applyMission'
import { findMission } from '@/lib/missions'
import { resetScope, useScope, emptyMembers } from '@/state/scope'
import { useSelection } from '@/state/selection'
import { useWorldState } from '@/v4/world/worldState'
import type { PanelKind } from '@/types'

const NOW = Date.parse('2026-09-26T12:00:00.000Z')

beforeEach(() => {
  resetScope()
  useSelection.getState().clear()
  useWorldState.setState({
    layers: {
      signals: true,
      findings: true,
      situations: true,
      entities: true,
      events: true,
    },
    spanMs: 24 * 3_600_000,
  })
})

describe('applyMission', () => {
  it('moves the map and the scrubber, not just the tab strip', () => {
    const crisis = findMission('crisis')!
    const applied = applyMission(crisis, () => true, { now: NOW })

    const world = useWorldState.getState()
    expect(world.spanMs).toBe(crisis.timeWindow.spanMs)
    expect(world.windowEndMs).toBe(NOW)
    expect(world.windowStartMs).toBe(NOW - crisis.timeWindow.spanMs)
    expect(applied.window).toEqual({
      startMs: NOW - crisis.timeWindow.spanMs,
      endMs: NOW,
      label: '6h',
    })
  })

  it('sets the claimed layers WHOLESALE — a previous mission\'s extras go off', () => {
    const crisis = findMission('crisis')!
    const result = applyMission(crisis, () => true, { now: NOW })
    expect(useWorldState.getState().layers).toEqual({
      signals: true,
      situations: true,
      events: true,
      findings: false,
      entities: false,
    })
    expect(result.layers).toEqual([...crisis.layers])
  })

  it('a mission with no layer claim leaves the layer selection untouched', () => {
    const before = { ...useWorldState.getState().layers }
    const applied = applyMission(findMission('release')!, () => true, { now: NOW })
    expect(useWorldState.getState().layers).toEqual(before)
    expect(applied.layers).toBeNull()
  })

  it('writes the scope BEFORE opening the tiles', () => {
    useSelection.getState().select({ kind: 'target', id: 'country_watch_il', label: 'Israel' })
    const seen: (string | null)[] = []
    const open = (kind: PanelKind) => {
      seen.push(`${kind}@${useScope.getState().scope?.id ?? 'none'}`)
      return true
    }
    applyMission(findMission('desk_watch')!, open, { now: NOW })
    // Every tile — Consult included — saw the new scope already in place.
    expect(seen.length).toBeGreaterThan(0)
    for (const entry of seen) expect(entry).toContain('@country_watch_il')
  })

  it('reports only the tiles the opener actually opened', () => {
    const declined: PanelKind[] = ['system.eval_scorecard']
    const applied = applyMission(
      findMission('release')!,
      (kind) => !declined.includes(kind),
      { now: NOW },
    )
    expect(applied.opened).toEqual(['system.consult'])
  })

  it('clears the scope for a world mission', () => {
    useScope.getState().setScope({
      kind: 'target',
      id: 'country_g20_ar',
      label: 'Argentina',
      members: emptyMembers(),
      origin: 'test',
    })
    const applied = applyMission(findMission('morning_read')!, () => true, { now: NOW })
    expect(useScope.getState().scope).toBeNull()
    expect(applied.scope.action).toBe('clear')
  })

  it('skipScope leaves the link\'s own scope in place and says it did', () => {
    useScope.getState().setScope({
      kind: 'target',
      id: 'country_g20_ar',
      label: 'Argentina',
      members: emptyMembers(),
      origin: 'share-link',
    })
    const applied = applyMission(findMission('morning_read')!, () => true, {
      now: NOW,
      skipScope: true,
    })
    expect(useScope.getState().scope?.id).toBe('country_g20_ar')
    expect(applied.scope.action).toBe('keep')
    expect(applied.scope.note).toContain("link's own scope was kept")
    expect(applied.scope.note).toContain('Argentina')
    // The window and the layers still applied — only the scope was deferred to.
    expect(applied.layers).toEqual([...findMission('morning_read')!.layers])
    expect(useWorldState.getState().layers.entities).toBe(false)
  })

  it('never throws when the opener refuses everything', () => {
    const noop = vi.fn(() => undefined)
    const applied = applyMission(findMission('crisis')!, noop, { now: NOW })
    expect(applied.opened).toEqual([])
    expect(noop).toHaveBeenCalled()
  })
})
