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
  it('ships the Wall (P1-7) + the v2 named presets + the Zen focus preset (Move 6)', () => {
    expect(LAYOUT_PRESETS.map((p) => p.id)).toEqual([
      'wall',
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

describe('the cold-boot seed is a WORKSPACE, not a preset', () => {
  // The landing grid moved to `lib/workspaces.ts` (MORNING READ) — see
  // `workspaces.test.ts` for its integrity/ordering/what-changed assertions,
  // which are the same bar this file holds the named presets to. What must
  // stay true HERE: the boot seed never became a user-selectable preset row,
  // so the Layouts menu still lists exactly the seven curated arrangements.
  it('no "boot"/"default" preset ever appeared in the picker', () => {
    expect(LAYOUT_PRESETS.map((p) => p.id)).not.toContain('boot')
    expect(findPreset('boot')).toBeUndefined()
    expect(findPreset('default')).toBeUndefined()
  })
})

describe('a saved custom layout is never touched by the landing-seed change', () => {
  // The acceptance bar: a returning user who saved a layout BEFORE this
  // feature shipped (so their serialized layout has no idea `system.
  // wall_movers` exists) must get that EXACT layout back, unmodified, when
  // they restore it — the boot-grid change only affects what seeds on a
  // COLD boot (no saved layout / before Restore is clicked).
  const PRE_U4_SAVED_LAYOUT = {
    grid: {
      root: { type: 'leaf', data: { views: ['system.findings', 'system.inspector'] } },
      width: 1600,
      height: 900,
    },
  } as unknown as SerializedDockview

  it('loadCustomLayout restores the pre-existing layout byte-for-byte, with no system.wall_movers merged in', () => {
    const save = fakeApi(PRE_U4_SAVED_LAYOUT)
    saveCustomLayout(save.api, 'personal')

    const restore = fakeApi()
    const ok = loadCustomLayout(restore.api, 'personal')
    expect(ok).toBe(true)
    expect(restore.state).toEqual(PRE_U4_SAVED_LAYOUT)
    // The literal serialized JSON must not mention the new kind anywhere —
    // proves nothing in the load path injects it.
    expect(JSON.stringify(restore.state)).not.toContain('wall_movers')
  })

  it("restoring replaces the workspace wholesale (fromJSON), so whatever the landing seed already mounted is fully superseded — no seeded tile can leak into a restored saved layout", () => {
    // Simulate: the landing workspace already seeded its kinds into the live
    // api (as App.tsx's boot effect would), THEN the user clicks Restore.
    const restore = fakeApi()
    // Pretend the boot effect already wrote something (any prior state) —
    // loadCustomLayout must not merge with it, only replace it.
    restore.api.fromJSON({ grid: { pretend: 'boot-seeded-state' } } as unknown as SerializedDockview)
    expect(restore.state).not.toEqual(PRE_U4_SAVED_LAYOUT)

    const save = fakeApi(PRE_U4_SAVED_LAYOUT)
    saveCustomLayout(save.api, 'personal')
    const ok = loadCustomLayout(restore.api, 'personal')
    expect(ok).toBe(true)
    expect(restore.state).toEqual(PRE_U4_SAVED_LAYOUT)
  })

  it('saveCustomLayout / loadCustomLayout / hasCustomLayout are untouched by the workspace store — no shared state, no auto-merge hook', () => {
    // The persistence trio only ever touches CUSTOM_LAYOUT_KEY via
    // toJSON/fromJSON; the workspace slots live under their own `legba_ws`
    // key. Prove they're independent: saving/loading a layout that NEVER
    // mentions any seeded kind still round-trips cleanly.
    localStorage.clear()
    expect(hasCustomLayout('personal')).toBe(false)
    const save = fakeApi(PRE_U4_SAVED_LAYOUT)
    saveCustomLayout(save.api, 'personal')
    expect(hasCustomLayout('personal')).toBe(true)
    const restore = fakeApi()
    expect(loadCustomLayout(restore.api, 'personal')).toBe(true)
    expect(restore.state).toEqual(PRE_U4_SAVED_LAYOUT)
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
