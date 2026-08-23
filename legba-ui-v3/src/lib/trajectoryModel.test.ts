/**
 * Tests for the situation-trajectory pure model.
 *
 * The route's honesty contract lives in its wire shape, and the one thing this
 * layer must not do is collapse the three distinct zero-states into one grey
 * "no data": a failed read, a known frame with an empty ledger, and a state
 * that has never been recorded are three different facts.
 */

import { describe, it, expect } from 'vitest'
import type { SituationTrajectory, TrajectoryEvent } from '@/lib/api'
import {
  currentState,
  deltaCounts,
  deltaLabel,
  deltaTone,
  eventWhen,
  trajectoryStatus,
  trajectoryStatusText,
} from './trajectoryModel'

function ev(over: Partial<TrajectoryEvent> = {}): TrajectoryEvent {
  return {
    id: 'e1',
    delta: 'escalates',
    occurred_at: '2026-08-10T00:00:00Z',
    state_from: 'watching',
    state_to: 'escalating',
    why: 'two new strikes',
    derived_from: ['f1'],
    source_output_id: 's1',
    created_at: '2026-08-10T01:00:00Z',
    ...over,
  }
}

function traj(over: Partial<SituationTrajectory> = {}): SituationTrajectory {
  return {
    situation_id: 'sit-1',
    name: 'Frame',
    state: 'escalating',
    events: [ev()],
    measured: true,
    ...over,
  }
}

describe('trajectoryStatus — the three zero-states stay distinct', () => {
  it('measured=false is "could not look", even with an empty list', () => {
    expect(trajectoryStatus(traj({ measured: false, events: [] }))).toBe('unmeasured')
  })

  it('measured=true with no events is "never assessed"', () => {
    expect(trajectoryStatus(traj({ events: [] }))).toBe('empty')
  })

  it('an undefined payload is unmeasured, not empty', () => {
    expect(trajectoryStatus(undefined)).toBe('unmeasured')
  })

  it('rows present reads as ok', () => {
    expect(trajectoryStatus(traj())).toBe('ok')
  })

  it('gives the two zero-states DIFFERENT operator sentences', () => {
    const a = trajectoryStatusText('unmeasured')
    const b = trajectoryStatusText('empty')
    expect(a).not.toBe(b)
    expect(a).toContain('could not look')
    expect(b).toContain('never been assessed')
    expect(trajectoryStatusText('ok')).toBe('')
  })
})

describe('currentState', () => {
  it('never invents a default state for a frame the ledger has not spoken about', () => {
    expect(currentState(traj({ state: null }))).toBeNull()
    expect(currentState(undefined)).toBeNull()
  })

  it('passes the recorded state through', () => {
    expect(currentState(traj({ state: 'watching' }))).toBe('watching')
  })
})

describe('eventWhen — evidence time vs write time', () => {
  it('prefers the evidence time and says so', () => {
    expect(eventWhen(ev())).toEqual({ iso: '2026-08-10T00:00:00Z', basis: 'evidence' })
  })

  it('falls back to the write time and LABELS the fallback', () => {
    expect(eventWhen(ev({ occurred_at: null }))).toEqual({
      iso: '2026-08-10T01:00:00Z',
      basis: 'recorded',
    })
  })

  it('reports an undated row as undated rather than picking a time', () => {
    expect(eventWhen(ev({ occurred_at: null, created_at: null }))).toEqual({
      iso: null,
      basis: 'none',
    })
  })
})

describe('deltaCounts — derived from the data, not a fixed vocabulary', () => {
  it('counts by delta, most frequent first', () => {
    const events = [
      ev({ id: '1', delta: 'escalates' }),
      ev({ id: '2', delta: 'escalates' }),
      ev({ id: '3', delta: 'broadens' }),
    ]
    expect(deltaCounts(events)).toEqual([
      { delta: 'escalates', n: 2 },
      { delta: 'broadens', n: 1 },
    ])
  })

  it('counts a delta the tracker has not shipped yet (tolerant of new kinds)', () => {
    expect(deltaCounts([ev({ delta: 'fragments' })])).toEqual([{ delta: 'fragments', n: 1 }])
  })

  it('returns nothing for an empty ledger', () => {
    expect(deltaCounts([])).toEqual([])
  })
})

describe('deltaLabel / deltaTone tolerate an unknown delta', () => {
  it('humanizes the known deltas', () => {
    expect(deltaLabel('de_escalates')).toBe('de-escalates')
    expect(deltaLabel('unchanged_checkpoint')).toBe('unchanged')
  })

  it('passes an unknown delta through instead of dropping it', () => {
    expect(deltaLabel('fragments')).toBe('fragments')
    expect(deltaTone('fragments')).toContain('border-line')
  })
})
