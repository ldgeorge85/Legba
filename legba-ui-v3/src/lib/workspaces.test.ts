/**
 * Tests for the six workspaces (UI_HOLISTIC_DESIGN_2026-08-24 §2 / §6.4).
 *
 * Three bars, mirroring the ones `layoutPresets.test.ts` holds the named
 * presets to, plus the two the stance model adds:
 *
 *  1. SEED INTEGRITY — every seeded kind is a real, non-binding singleton and
 *     every non-anchor placement references a panel named EARLIER in the same
 *     seed (the invariant that keeps `addPanel({position})` from naming a
 *     panel Dockview has never seen).
 *  2. THE LANDING still answers "what changed" (U-4's acceptance, carried
 *     forward): Morning Read mounts the surface that owns
 *     movers-since-last-visit, plus every other pre-existing boot surface.
 *  3. WORKSPACES ARE OBJECTS, NOT RESETS — one slot per workspace per mode,
 *     switching serializes the outgoing stance, and the legacy single-slot
 *     Save/Restore layout is COPIED (never moved, never clobbered) on the
 *     first boot that has no workspace store.
 *
 * Uses the same DOM-free fake DockviewApi idiom as `layoutPresets.test.ts`.
 */

import { describe, it, expect, beforeEach } from 'vitest'
import type { DockviewApi, SerializedDockview } from 'dockview-react'
import { PANEL_REGISTRY } from '@/panel-registry/registry'
import type { PanelKind } from '@/types'
import {
  LANDING_WORKSPACE,
  WORKSPACES,
  WS_SCHEMA_VERSION,
  findWorkspace,
  hasWorkspaceLayout,
  isWorkspaceId,
  loadWorkspaceLayout,
  migrateLegacyLayout,
  resetWorkspaceLayout,
  saveWorkspaceLayout,
  seedWorkspace,
  type WorkspaceId,
} from './workspaces'

const WS_KEY = 'legba_ws'
const LEGACY_KEY = 'legba_layout_custom'

beforeEach(() => localStorage.clear())

/** A fake DockviewApi exposing just the surface the helpers call. */
function fakeApi(initial?: SerializedDockview) {
  let state: SerializedDockview | undefined = initial
  const api = {
    clear: () => {},
    toJSON: () => state ?? ({ grid: {} } as unknown as SerializedDockview),
    fromJSON: (data: SerializedDockview) => {
      state = data
    },
  } as unknown as DockviewApi
  return {
    api,
    get state() {
      return state
    },
  }
}

describe('the six stances', () => {
  it('ships exactly the six the design names, in bar order', () => {
    expect(WORKSPACES.map((w) => w.id)).toEqual([
      'morning_read',
      'desk',
      'investigate',
      'trust',
      'gate',
      'engine',
    ])
    expect(WORKSPACES.map((w) => w.label)).toEqual([
      'Morning Read',
      'Desk',
      'Investigate',
      'Trust',
      'The Gate',
      'Engine',
    ])
  })

  it('numbers them 1..6 so Alt+<index> is unambiguous', () => {
    expect(WORKSPACES.map((w) => w.index)).toEqual([1, 2, 3, 4, 5, 6])
  })

  it('every stance states the question it answers (the bar tooltip / ⌘K haystack)', () => {
    for (const ws of WORKSPACES) {
      expect(ws.question.length, ws.id).toBeGreaterThan(10)
    }
  })

  it('lands on Morning Read', () => {
    expect(LANDING_WORKSPACE).toBe('morning_read')
    expect(findWorkspace(LANDING_WORKSPACE)).toBeDefined()
  })

  it('findWorkspace / isWorkspaceId resolve known ids and reject unknown', () => {
    expect(findWorkspace('trust')?.label).toBe('Trust')
    expect(findWorkspace('nope')).toBeUndefined()
    expect(isWorkspaceId('gate')).toBe(true)
    expect(isWorkspaceId('wall')).toBe(false)
  })
})

describe('seed integrity', () => {
  it('every seeded kind is a real, non-binding singleton', () => {
    for (const ws of WORKSPACES) {
      for (const placement of ws.seed) {
        const entry = PANEL_REGISTRY[placement.kind]
        expect(entry, `${ws.id} → ${placement.kind}`).toBeDefined()
        expect(entry.definition.requiresBinding, `${ws.id} → ${placement.kind}`).toBe(false)
      }
    }
  })

  it('the anchor comes first and every later placement references an earlier panel', () => {
    for (const ws of WORKSPACES) {
      const seen = new Set<string>()
      ws.seed.forEach((placement, i) => {
        if (i === 0) expect(placement.position, `${ws.id} anchor`).toBeUndefined()
        else {
          expect(placement.position, `${ws.id} → ${placement.kind}`).toBeDefined()
          expect(seen.has(placement.position!.referencePanel), `${ws.id} → ${placement.kind}`).toBe(
            true,
          )
        }
        seen.add(placement.kind)
      })
    }
  })

  it('no stance seeds the same kind twice (a dedup would silently drop the second placement)', () => {
    for (const ws of WORKSPACES) {
      const kinds = ws.seed.map((p) => p.kind)
      expect(new Set(kinds).size, ws.id).toBe(kinds.length)
    }
  })

  it('every `active` / size hint names a kind the same stance actually seeds', () => {
    for (const ws of WORKSPACES) {
      const kinds = new Set(ws.seed.map((p) => p.kind))
      for (const kind of ws.active ?? []) expect(kinds.has(kind), `${ws.id} active ${kind}`).toBe(true)
      for (const hint of ws.sizes ?? []) expect(kinds.has(hint.kind), `${ws.id} size ${hint.kind}`).toBe(true)
    }
  })

  it('the Inspector — the keystone of findings reachability — is mounted by EVERY stance', () => {
    for (const ws of WORKSPACES) {
      expect(ws.seed.map((p) => p.kind), ws.id).toContain('system.inspector')
    }
  })
})

describe('the landing (Morning Read) still answers "what changed"', () => {
  const morning = findWorkspace('morning_read')!
  const kinds = morning.seed.map((p) => p.kind)

  it('mounts the Wall — the surface that OWNS movers-since-last-visit', () => {
    // U-4's acceptance, carried forward: the old boot grid mounted
    // `system.wall_movers`, a standalone copy of the Wall's own quadrant,
    // beside its own parent. The Wall itself carries movers + the band grid +
    // newest verified + the health corner (design §1.2 / §2.4 #1).
    expect(kinds).toContain('system.wall')
    expect(kinds).not.toContain('system.wall_movers')
  })

  it('keeps every other pre-existing boot surface on the first screenful', () => {
    expect(kinds).toEqual(
      expect.arrayContaining([
        'v4.kpi',
        'system.findings',
        'v4.map',
        'v4.assessment',
        'system.inspector',
      ]),
    )
  })

  it('puts the glance strip above the Wall, and the feed below it', () => {
    expect(morning.seed[0]).toEqual({ kind: 'v4.kpi' })
    const wall = morning.seed.find((p) => p.kind === 'system.wall')!
    expect(wall.position).toEqual({ referencePanel: 'v4.kpi', direction: 'below' })
    const feed = morning.seed.find((p) => p.kind === 'system.findings')!
    expect(feed.position).toEqual({ referencePanel: 'system.wall', direction: 'below' })
  })

  it('opens on the World Assessment, not the Inspector (the report is the read; the Inspector waits for a selection)', () => {
    expect(morning.active).toContain('v4.assessment')
    expect(morning.active).not.toContain('system.inspector')
  })
})

describe('seedWorkspace', () => {
  it('walks the seed in order through the opener', () => {
    const calls: PanelKind[] = []
    const def = findWorkspace('investigate')!
    seedWorkspace(def, (kind) => {
      calls.push(kind)
      return true
    })
    expect(calls).toEqual(def.seed.map((p) => p.kind))
  })

  it('passes each placement its position verbatim once the reference is open', () => {
    const positions: Array<string | undefined> = []
    const def = findWorkspace('desk')!
    seedWorkspace(def, (_kind, position) => {
      positions.push(position?.referencePanel)
      return true
    })
    expect(positions).toEqual(def.seed.map((p) => p.position?.referencePanel))
  })

  it('re-anchors a placement whose reference was skipped (mode gating drops operator panels in cis)', () => {
    // The opener returns undefined for a gated kind — exactly what App.tsx's
    // `addSingleton` does when `def.modes` excludes the active mode. The next
    // placement must NOT be handed a position naming a panel that never opened.
    const def = findWorkspace('engine')!
    const seen: Array<{ kind: PanelKind; ref?: string }> = []
    const opened = seedWorkspace(def, (kind, position) => {
      seen.push({ kind, ref: position?.referencePanel })
      // Skip the anchor to force the re-anchor path.
      return kind === def.seed[0].kind ? undefined : true
    })
    expect(opened).not.toContain(def.seed[0].kind)
    for (const row of seen) {
      if (row.ref) expect(row.ref).not.toBe(def.seed[0].kind)
    }
  })

  it('returns only the kinds that actually opened', () => {
    const def = findWorkspace('trust')!
    const opened = seedWorkspace(def, (kind) => (kind === 'system.inspector' ? undefined : true))
    expect(opened).not.toContain('system.inspector')
    expect(opened.length).toBe(def.seed.length - 1)
  })
})

describe('workspaces are objects, not resets', () => {
  const LAYOUT = { grid: { width: 100 } } as unknown as SerializedDockview

  it('stores one slot per workspace per mode, stamped with the schema version', () => {
    const save = fakeApi(LAYOUT)
    saveWorkspaceLayout(save.api, 'trust', 'personal')

    const store = JSON.parse(localStorage.getItem(WS_KEY)!)
    expect(store.trust.personal.schemaVersion).toBe(WS_SCHEMA_VERSION)
    expect(store.trust.personal.layout).toEqual(LAYOUT)
    expect(hasWorkspaceLayout('trust', 'personal')).toBe(true)
    // A different stance, and the same stance in another mode, stay empty.
    expect(hasWorkspaceLayout('gate', 'personal')).toBe(false)
    expect(hasWorkspaceLayout('trust', 'cis')).toBe(false)
  })

  it('round-trips a stance layout back through fromJSON', () => {
    const save = fakeApi(LAYOUT)
    saveWorkspaceLayout(save.api, 'desk', 'personal')
    const restore = fakeApi()
    expect(loadWorkspaceLayout(restore.api, 'desk', 'personal')).toBe(true)
    expect(restore.state).toEqual(LAYOUT)
  })

  it('switching away and back returns the OUTGOING arrangement, not a re-seed (the bug that made presets unusable)', () => {
    const morning = { grid: { tag: 'morning-as-i-left-it' } } as unknown as SerializedDockview
    const trust = { grid: { tag: 'trust-as-i-left-it' } } as unknown as SerializedDockview
    // Leave Morning Read → its live layout is serialized into its own slot.
    saveWorkspaceLayout(fakeApi(morning).api, 'morning_read', 'personal')
    // Work in Trust, leave it too.
    saveWorkspaceLayout(fakeApi(trust).api, 'trust', 'personal')
    // Come back to Morning Read.
    const back = fakeApi()
    expect(loadWorkspaceLayout(back.api, 'morning_read', 'personal')).toBe(true)
    expect(back.state).toEqual(morning)
    // Trust's slot is untouched by the round trip.
    const backToTrust = fakeApi()
    loadWorkspaceLayout(backToTrust.api, 'trust', 'personal')
    expect(backToTrust.state).toEqual(trust)
  })

  it('loadWorkspaceLayout returns false when the stance has never been saved (→ the caller seeds the curated default)', () => {
    const restore = fakeApi()
    expect(loadWorkspaceLayout(restore.api, 'gate', 'personal')).toBe(false)
    expect(restore.state).toBeUndefined()
  })

  it('resetWorkspaceLayout drops only that stance/mode slot', () => {
    saveWorkspaceLayout(fakeApi(LAYOUT).api, 'engine', 'personal')
    saveWorkspaceLayout(fakeApi(LAYOUT).api, 'engine', 'cis')
    saveWorkspaceLayout(fakeApi(LAYOUT).api, 'gate', 'personal')
    resetWorkspaceLayout('engine', 'personal')
    expect(hasWorkspaceLayout('engine', 'personal')).toBe(false)
    expect(hasWorkspaceLayout('engine', 'cis')).toBe(true)
    expect(hasWorkspaceLayout('gate', 'personal')).toBe(true)
  })

  it('tolerates corrupt localStorage', () => {
    localStorage.setItem(WS_KEY, '{bad json')
    expect(hasWorkspaceLayout('desk', 'personal')).toBe(false)
    const restore = fakeApi()
    expect(loadWorkspaceLayout(restore.api, 'desk', 'personal')).toBe(false)
  })
})

describe('the legacy saved layout is adopted, never clobbered', () => {
  const LEGACY = {
    grid: { root: { type: 'leaf', data: { views: ['system.findings'] } } },
  } as unknown as SerializedDockview

  function writeLegacy(mode: string, layout: SerializedDockview) {
    localStorage.setItem(LEGACY_KEY, JSON.stringify({ [mode]: layout }))
  }

  it('a pre-workspace saved layout becomes the Morning Read slot on a first boot with no workspace store', () => {
    writeLegacy('personal', LEGACY)
    expect(migrateLegacyLayout('personal')).toBe(true)
    const restore = fakeApi()
    expect(loadWorkspaceLayout(restore.api, LANDING_WORKSPACE, 'personal')).toBe(true)
    expect(restore.state).toEqual(LEGACY)
  })

  it('COPIES it — `legba_layout_custom` is left byte-identical, so Save/Restore keeps working', () => {
    writeLegacy('personal', LEGACY)
    const before = localStorage.getItem(LEGACY_KEY)
    migrateLegacyLayout('personal')
    expect(localStorage.getItem(LEGACY_KEY)).toBe(before)
  })

  it('never overwrites an existing workspace store (a second boot is a no-op)', () => {
    writeLegacy('personal', LEGACY)
    const mine = { grid: { tag: 'mine' } } as unknown as SerializedDockview
    saveWorkspaceLayout(fakeApi(mine).api, LANDING_WORKSPACE, 'personal')
    expect(migrateLegacyLayout('personal')).toBe(false)
    const restore = fakeApi()
    loadWorkspaceLayout(restore.api, LANDING_WORKSPACE, 'personal')
    expect(restore.state).toEqual(mine)
  })

  it('is a no-op when there is no legacy layout for this mode', () => {
    writeLegacy('cis', LEGACY)
    expect(migrateLegacyLayout('personal')).toBe(false)
    expect(hasWorkspaceLayout(LANDING_WORKSPACE, 'personal')).toBe(false)
  })

  it('tolerates a corrupt legacy blob', () => {
    localStorage.setItem(LEGACY_KEY, '{bad json')
    expect(migrateLegacyLayout('personal')).toBe(false)
  })
})

describe('§6.5 the stance rule — every registered kind has a declared home', () => {
  // The design's replacement for `navGroups.test.ts`'s row budget: a panel
  // kind may not exist without a stance that mounts it (or an explicit
  // reason). Stated here as the SEED side of the rule — the sidebar catalog
  // is the other half (a kind is always reachable from it), so this asserts
  // the positive direction: every kind a workspace names is registered, and
  // the six seeds between them cover the daily spine.
  it('the daily spine is mounted by at least one stance', () => {
    const seeded = new Set<PanelKind>(WORKSPACES.flatMap((w) => w.seed.map((p) => p.kind)))
    for (const kind of [
      'system.wall',
      'system.findings',
      'system.inspector',
      'v4.map',
      'system.timeline',
      'v4.assessment',
      'system.journal',
      'system.consult',
    ] as PanelKind[]) {
      expect(seeded.has(kind), `${kind} is mounted by no workspace`).toBe(true)
    }
  })

  it('the human-gated queues have a named front door (the standing journal rule)', () => {
    const gate = findWorkspace('gate')!.seed.map((p) => p.kind)
    expect(gate).toContain('system.journal_gate')
    expect(gate).toContain('system.goldset')
    expect(gate).toContain('system.optimizer')
  })

  it('the GLASS-3 ops deck is composed by Trust, not scattered across Engine Room rows', () => {
    const trust = findWorkspace('trust')!.seed.map((p) => p.kind)
    for (const kind of [
      'system.production_gauge',
      'system.judge_stats',
      'system.source_health',
      'system.eval_boards',
    ] as PanelKind[]) {
      expect(trust).toContain(kind)
    }
  })
})

describe('workspace ids are stable storage keys', () => {
  it('every id is a plain lowercase token (it keys localStorage and the share hash)', () => {
    for (const ws of WORKSPACES) {
      expect(ws.id, ws.label).toMatch(/^[a-z_]+$/)
    }
  })

  it('the id set is exactly what isWorkspaceId accepts', () => {
    const ids: WorkspaceId[] = WORKSPACES.map((w) => w.id)
    for (const id of ids) expect(isWorkspaceId(id)).toBe(true)
  })
})
