/**
 * Unit tests for the command-palette recents + favorites store.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import {
  loadRecents,
  pushRecent,
  loadFavorites,
  toggleFavorite,
} from './paletteRecents'

beforeEach(() => {
  localStorage.clear()
})

describe('paletteRecents', () => {
  it('starts empty', () => {
    expect(loadRecents()).toEqual([])
    expect(loadFavorites().size).toBe(0)
  })

  it('pushes recents newest-first, deduped', () => {
    pushRecent('panel:system.findings')
    pushRecent('record:target:brazil')
    pushRecent('panel:system.findings') // re-touch floats it to front
    expect(loadRecents()).toEqual(['panel:system.findings', 'record:target:brazil'])
  })

  it('caps recents at 12', () => {
    for (let i = 0; i < 20; i++) pushRecent(`entry:${i}`)
    const recents = loadRecents()
    expect(recents).toHaveLength(12)
    // Newest (entry:19) is first; oldest retained is entry:8.
    expect(recents[0]).toBe('entry:19')
    expect(recents).not.toContain('entry:7')
  })

  it('ignores an empty entry id', () => {
    pushRecent('')
    expect(loadRecents()).toEqual([])
  })

  it('toggles favorites on and off', () => {
    expect(toggleFavorite('record:analyst:country_assessor')).toBe(true)
    expect(loadFavorites().has('record:analyst:country_assessor')).toBe(true)
    expect(toggleFavorite('record:analyst:country_assessor')).toBe(false)
    expect(loadFavorites().has('record:analyst:country_assessor')).toBe(false)
  })

  it('degrades to empty on corrupt storage', () => {
    localStorage.setItem('legba_palette_recents', '{not json')
    expect(loadRecents()).toEqual([])
  })
})
