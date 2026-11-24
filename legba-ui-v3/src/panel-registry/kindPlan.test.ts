/**
 * THE RATCHETS THAT REPLACED THE TREE'S BUDGET (UI-B1/shell; UI-D1 §2.2).
 *
 * `navGroups.test.ts`'s `BUDGET = 26` was one of the two places in the repo
 * that said "no" to panel growth. The commit that deleted the tree replaced it
 * with these, so growth is never unguarded for a wave:
 *
 *   1. panel rows in the rail: 26 → **0** (`components/ObjectRail.test.tsx`
 *      renders the rail and counts them; here, the data side: the rail has no
 *      panel-catalog source to render rows from);
 *   2. registry kinds **≤ 33** OFF Table B's retirement schedule (Table A's 31
 *      survivors + 2 headroom) — a new kind counts against 33 the day it lands;
 *      the total may only fall from 62 as the merge lanes delete scheduled rows;
 *   3. `registry.ts` exports no `HIDDEN_KINDS` — a drill is DERIVED;
 *   4. THE SURFACE RULE (holistic §6.5, re-cut by the U-2 shell): every kind
 *      appears in a surface's regions, is frame (Consult, the Record sheet),
 *      is in the alias table (including Table B's scheduled rows), or is in the
 *      drill list with a declared parent.
 */
import { describe, expect, it } from 'vitest'
import type { PanelKind } from '@/types'
import * as registryModule from './registry'
import { PANEL_REGISTRY } from './registry'
import { KIND_ALIASES } from './aliases'
import {
  PLANNED_SURVIVORS,
  REGISTRY_SURVIVOR_CEILING,
  REGISTRY_TOTAL_CEILING,
  RETIREMENT_SCHEDULE,
  SINGLETON_DRILLS,
  catalogKinds,
  drillParent,
  drillsOf,
  isDrill,
  landedRetirements,
  pendingRetirements,
  survivorKinds,
} from './kindPlan'
import { SURFACES, surfaceKinds } from '@/lib/surfaces'

const ALL = Object.keys(PANEL_REGISTRY) as PanelKind[]

describe('ratchet — registry kinds ≤ 33 off the retirement schedule', () => {
  it(`counts ${REGISTRY_SURVIVOR_CEILING} at most (UI-D1 §2.2: 31 survivors + 2 headroom)`, () => {
    const survivors = survivorKinds()
    expect(survivors.length, survivors.join(', ')).toBeLessThanOrEqual(REGISTRY_SURVIVOR_CEILING)
  })

  it('the TOTAL never rises above its ceiling — 62 before the plan, 45 after UI-B1/merges, 38 after UI-B3\'s subject half, 39 with N-J\'s system.flow', () => {
    expect(ALL.length).toBeLessThanOrEqual(REGISTRY_TOTAL_CEILING)
    expect(REGISTRY_TOTAL_CEILING).toBe(39)
  })

  it('UI-B1/merges PINS its count: 62 − the twenty rows it landed + the three Table A kinds it built = 45', () => {
    const landedByMerges = landedRetirements().filter((k) => RETIREMENT_SCHEDULE[k].lands === 'UI-B1/merges')
    expect(landedByMerges).toHaveLength(20)
    for (const built of ['system.registry', 'system.ledgers', 'system.register'] as PanelKind[]) {
      expect(PANEL_REGISTRY[built], built).toBeDefined()
    }
    // The lane leaves none of its own rows pending.
    expect(pendingRetirements().filter((k) => RETIREMENT_SCHEDULE[k].lands === 'UI-B1/merges')).toEqual([])
  })

  it('UI-B3 (subject half) PINS the count: 45 − the eight per-subject reads + subject.page = 38', () => {
    const toSubject = Object.keys(RETIREMENT_SCHEDULE).filter((k) => RETIREMENT_SCHEDULE[k].survivor === 'subject.page')
    expect(toSubject.sort()).toEqual([
      'target.claims',
      'target.desk_brief_page',
      'target.findings',
      'target.graph',
      'target.overview',
      'target.signals',
      'target.situations',
      'target.sources',
    ])
    for (const k of toSubject) expect(RETIREMENT_SCHEDULE[k].retired, k).toBe(true)
    expect(PANEL_REGISTRY['subject.page']).toBeDefined()
    const landedByMerges = landedRetirements().filter((k) => RETIREMENT_SCHEDULE[k].lands === 'UI-B1/merges')
    // + 1: N-J's `system.flow` (the read-only descriptor canvas, 2026-11-13).
    expect(ALL.length).toBe(62 - landedByMerges.length + 3 - toSubject.length + 1 + 1)
    expect(ALL.length).toBe(REGISTRY_TOTAL_CEILING)
  })

  it('every PENDING retirement is a real, registered kind (a stale row would hide growth)', () => {
    for (const kind of pendingRetirements()) {
      expect(PANEL_REGISTRY[kind as PanelKind], kind).toBeDefined()
    }
  })

  it('every LANDED retirement is gone from the registry and in the alias table, on the same survivor and tab', () => {
    for (const kind of landedRetirements()) {
      const row = RETIREMENT_SCHEDULE[kind]
      expect(PANEL_REGISTRY[kind as PanelKind], `${kind} still registered`).toBeUndefined()
      expect(KIND_ALIASES[kind], `${kind} has no alias row`).toEqual(
        row.tab ? { kind: row.survivor, tab: row.tab } : { kind: row.survivor },
      )
    }
  })

  it('every scheduled survivor is registered, planned (Table A), or the palette', () => {
    const planned = new Set<string>(PLANNED_SURVIVORS)
    for (const [kind, row] of Object.entries(RETIREMENT_SCHEDULE)) {
      const ok =
        !!PANEL_REGISTRY[row.survivor as PanelKind] || planned.has(row.survivor) || row.survivor.startsWith('palette:')
      expect(ok, `${kind} → ${row.survivor}`).toBe(true)
      // A survivor is never itself scheduled to retire (no alias chains).
      expect(RETIREMENT_SCHEDULE[row.survivor], `${kind} → ${row.survivor} retires too`).toBeUndefined()
    }
  })

  it('no PENDING id is already in the alias table (a row moves, it is never in both)', () => {
    for (const kind of pendingRetirements()) expect(KIND_ALIASES[kind], kind).toBeUndefined()
  })

  it('Table B schedules 36 retirements; the survivors arrive at 31 with the new kinds still planned', () => {
    expect(Object.keys(RETIREMENT_SCHEDULE)).toHaveLength(36)
    // `subject.page` left the planned list when UI-B3 built it (W11).
    expect(PLANNED_SURVIVORS).toEqual(['unit.page'])
    // 31 = UI-D1 Table A; + 1 = N-J's `system.flow`, a survivor Table A did not plan.
    expect(survivorKinds().length + PLANNED_SURVIVORS.length).toBe(32)
  })
})

describe('ratchet — no HIDDEN_KINDS; a drill is derived', () => {
  it('registry.ts exports no HIDDEN_KINDS and no definition carries a hidden flag', () => {
    expect('HIDDEN_KINDS' in registryModule).toBe(false)
    for (const kind of ALL) {
      expect('hidden' in PANEL_REGISTRY[kind].definition, kind).toBe(false)
    }
  })

  it('a drill is a kind whose params need a ref: every bound kind, plus the declared singleton drills', () => {
    for (const kind of ALL) {
      const bound = PANEL_REGISTRY[kind].definition.requiresBinding
      expect(isDrill(kind), kind).toBe(bound || kind in SINGLETON_DRILLS)
    }
  })

  it('every drill declares its parent object', () => {
    for (const kind of ALL.filter(isDrill)) {
      const parent = drillParent(kind)!
      expect(['subject', 'unit', 'claim', 'candidate']).toContain(parent.object)
      if (parent.object === 'claim' || parent.object === 'candidate') {
        for (const from of parent.from) expect(PANEL_REGISTRY[from], `${kind} ← ${from}`).toBeDefined()
      }
    }
    expect(drillsOf('subject').length).toBeGreaterThan(0)
    expect(drillsOf('unit').length).toBeGreaterThan(0)
  })

  it('the `＋` catalog is exactly the non-drills — never a drill', () => {
    expect(catalogKinds().sort()).toEqual(ALL.filter((k) => !isDrill(k)).sort())
    for (const k of catalogKinds()) expect(isDrill(k)).toBe(false)
  })
})

describe('THE SURFACE RULE (holistic §6.5; U-2) — every kind has a declared home', () => {
  const seeded = new Set<string>(SURFACES.flatMap((sf) => surfaceKinds(sf)))
  // Frame, not regions: the Consult column and the Record sheet.
  const frame = new Set<string>(['system.consult', 'system.inspector'])
  const aliasSurvivors = new Set<string>(Object.values(KIND_ALIASES).map((a) => a.kind))

  it('every registered kind is in a surface, the frame, the alias table / Table B, or the drill list', () => {
    const homeless = ALL.filter(
      (k) => !seeded.has(k) && !frame.has(k) && !aliasSurvivors.has(k) && !RETIREMENT_SCHEDULE[k] && !isDrill(k),
    )
    expect(homeless, `kinds with no declared home: ${homeless.join(', ')}`).toEqual([])
  })

  it('the rule is not vacuous: every survivor off the schedule is seeded, frame, aliased-to or a drill', () => {
    for (const k of survivorKinds()) {
      expect(seeded.has(k) || frame.has(k) || aliasSurvivors.has(k) || isDrill(k), k).toBe(true)
    }
  })

  it('no surface seeds the frame: Consult and the Record sheet are never a region', () => {
    for (const k of frame) expect(seeded.has(k), k).toBe(false)
  })
})
