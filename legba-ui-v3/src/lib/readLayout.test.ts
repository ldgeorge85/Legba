/**
 * readLayout — the persisted ordering flip.
 *
 * Ruling 3 has two halves and both are testable: Layout B is the DEFAULT, and
 * the flip is CHEAP. "Cheap" here means it survives a remount without a fetch —
 * which is exactly what a module-level store plus localStorage buys, and what
 * component state would not.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import {
  DEFAULT_READ_LAYOUT,
  __resetReadLayout,
  getReadLayout,
  setReadLayout,
  subscribeReadLayout,
  toggleReadLayout,
} from './readLayout'

beforeEach(() => {
  __resetReadLayout()
})

describe('readLayout', () => {
  it('defaults to B — evidence map first, Assessment last (ruling 3)', () => {
    expect(DEFAULT_READ_LAYOUT).toBe('B')
    expect(getReadLayout()).toBe('B')
  })

  it('writes the choice through to storage, so a reload keeps it', () => {
    setReadLayout('A')
    expect(localStorage.getItem('legba_read_layout')).toBe('A')
    expect(getReadLayout()).toBe('A')
  })

  it('toggles between exactly the two orderings', () => {
    expect(toggleReadLayout()).toBe('A')
    expect(toggleReadLayout()).toBe('B')
  })

  it('notifies subscribers on a real change and not on a no-op', () => {
    let calls = 0
    const off = subscribeReadLayout(() => {
      calls += 1
    })
    setReadLayout('A')
    expect(calls).toBe(1)
    setReadLayout('A')
    expect(calls).toBe(1)
    off()
    setReadLayout('B')
    expect(calls).toBe(1)
  })

  it('falls back to the default when storage holds junk', () => {
    // `__resetReadLayout` has already dropped the cached value, so this read
    // goes to storage — which is where a hand-edited or stale key would be.
    localStorage.setItem('legba_read_layout', 'sideways')
    expect(getReadLayout()).toBe('B')
  })
})
