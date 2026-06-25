/**
 * Unit tests for layout presets — preset integrity, mode-gated seeding via
 * a stub opener, and custom-layout round-trip through localStorage.
 *
 * Uses a minimal fake DockviewApi (only the members the helpers touch:
 * clear / toJSON / fromJSON) so the tests stay DOM-free.
 */

import { describe, it, expect, beforeEach } from 'vitest'
import type { DockviewApi, SerializedDockview } from 'dockview-react'
import {
  LAYOUT_PRESETS,
  applyPreset,
  findPreset,
  hasCustomLayout,
  loadCustomLayout,
  saveCustomLayout,
  type SingletonOpener,
} from './layoutPresets'
import { PANEL_REGISTRY } from '@/panel-registry/registry'

beforeEach(() => localStorage.clear())

/** A fake DockviewApi exposing just the surface the helpers call. */
function fakeApi(initial?: SerializedDockview) {
  let state: SerializedDockview | undefined = initial
  let cleared = 0
  const api = {
    clear: () => {
      cleared++
    },
    toJSON: () => state ?? ({ grid: {} } as unknown as SerializedDockview),
    fromJSON: (data: SerializedDockview) => {
      state = data
    },
  } as unknown as DockviewApi
  return {
    api,
    get cleared() {
      return cleared
    },
    get state() {
      return state
    },
  }
}

describe('layout presets', () => {
  it('ships the v2 named presets + the Zen focus preset (Move 6)', () => {
    expect(LAYOUT_PRESETS.map((p) => p.id)).toEqual([
      'monitoring',
      'workspace',
      'investigation',
      'analysis',
      'operations',
      'focus',
    ])
  })

  it('every preset panel kind is a real, non-binding singleton', () => {
    for (const preset of LAYOUT_PRESETS) {
      for (const placement of preset.panels) {
        const entry = PANEL_REGISTRY[placement.kind]
        expect(entry, `${preset.id} → ${placement.kind}`).toBeDefined()
        expect(entry.definition.requiresBinding).toBe(false)
      }
    }
  })

  it('non-anchor placements reference an earlier panel in the same preset', () => {
    for (const preset of LAYOUT_PRESETS) {
      const seen = new Set<string>()
      preset.panels.forEach((placement, i) => {
        if (i === 0) {
          expect(placement.position, `${preset.id} anchor`).toBeUndefined()
        } else {
          expect(placement.position).toBeDefined()
          expect(seen.has(placement.position!.referencePanel)).toBe(true)
        }
        seen.add(placement.kind)
      })
    }
  })

  it('findPreset resolves known ids and rejects unknown', () => {
    expect(findPreset('monitoring')?.label).toBe('Monitoring')
    expect(findPreset('nope')).toBeUndefined()
  })

  it('applyPreset seeds via the opener in placement order', () => {
    const { api } = fakeApi()
    const calls: string[] = []
    const open: SingletonOpener = (kind) => calls.push(kind)
    const preset = findPreset('monitoring')!
    applyPreset(api, preset, open)
    expect(calls).toEqual(preset.panels.map((p) => p.kind))
  })

  it('applyPreset issues exactly one clear', () => {
    const fake = fakeApi()
    applyPreset(fake.api, findPreset('operations')!, () => {})
    expect(fake.cleared).toBe(1)
  })
})

describe('custom layout persistence', () => {
  it('round-trips a layout per mode through localStorage', () => {
    const layout = { grid: { width: 100 } } as unknown as SerializedDockview
    const save = fakeApi(layout)
    saveCustomLayout(save.api, 'personal')

    expect(hasCustomLayout('personal')).toBe(true)
    expect(hasCustomLayout('cis')).toBe(false)

    const restore = fakeApi()
    const ok = loadCustomLayout(restore.api, 'personal')
    expect(ok).toBe(true)
    expect(restore.state).toEqual(layout)
  })

  it('loadCustomLayout returns false when nothing is saved', () => {
    const restore = fakeApi()
    expect(loadCustomLayout(restore.api, 'personal')).toBe(false)
  })

  it('tolerates corrupt localStorage', () => {
    localStorage.setItem('legba_layout_custom', '{bad json')
    expect(hasCustomLayout('personal')).toBe(false)
    const restore = fakeApi()
    expect(loadCustomLayout(restore.api, 'personal')).toBe(false)
  })
})
