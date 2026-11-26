/**
 * UI-B1/merges (W8) — the registry 62 → 45 cut, proved end to end on the data
 * and the executor (UI-D1 §2.5 Table B, §8 row "UI-B1/merges").
 *
 *   1. every id the lane retired or re-pointed (23: Table B's twenty + the
 *      three "re-pointed, not new") lands, through THE ONE EXECUTOR's alias
 *      path, on its survivor AT THE RIGHT TAB — `open_panel(<retired>)` asks
 *      the shell for `(survivor, tab)`, for the operator and (where the drive
 *      pack allows the id) for a case;
 *   2. ZERO component files deleted: every component a retired kind rendered
 *      is still in the bundle's source tree (the merged wrappers mount them);
 *   3. a retired id's own descriptor-facing panel_id resolves the same way.
 *
 * The count itself is pinned in `kindPlan.test.ts` (REGISTRY_TOTAL_CEILING =
 * 45, exact) and the tabs' mounting in `panels/merged/merges.test.tsx`.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, applyAction, type ShellPort } from '@/lib/applyAction'
import { CASE_VOCABULARY, isDrivableKind } from '@/lib/caseModel'
import { pendingPanelParams, resetPanelParams } from '@/state/panelParams'
import { KIND_ALIASES, resolveRetiredPanelId } from './aliases'
import { PANEL_REGISTRY } from './registry'
import type { PanelKind } from '@/types'

function fakeShell(mode = 'personal') {
  return {
    mode,
    openSingleton: vi.fn(() => ({})),
    openBound: vi.fn(() => true),
    switchStance: vi.fn(),
    applyPreset: vi.fn(),
    runMission: vi.fn(),
    reopenConsult: vi.fn(),
    currentStance: vi.fn(() => 'morning_read'),
    serializeLayout: vi.fn(() => null),
    restoreLayout: vi.fn(),
  } satisfies ShellPort
}

/** The 23 ids this lane retired (20) or re-pointed (3), with their landing. */
const LANDINGS: ReadonlyArray<[string, PanelKind, string | undefined]> = [
  // → system.registry
  ['registry.targets', 'system.registry', 'subjects'],
  ['registry.analysts', 'system.registry', 'units'],
  ['registry.sources', 'system.registry', 'sources'],
  ['source.detail', 'system.registry', 'sources'],
  ['registry.stack', 'system.registry', 'stack'],
  ['registry.action_packs', 'system.registry', 'packs'],
  ['source.subscription_builder', 'system.registry', 'builder'],
  ['source.subscription_policy', 'system.registry', 'policy'],
  ['v4.flow', 'system.registry', 'canvas'],
  // → system.status
  ['system.production_gauge', 'system.status', 'production'],
  ['system.dead_letter', 'system.status', 'failures'],
  ['system.actor_health', 'system.status', 'actors'],
  ['system.stream_lag', 'system.status', 'queues'],
  // → system.ledgers
  ['system.governor', 'system.ledgers', 'governor'],
  ['system.audit', 'system.ledgers', 'audit'],
  ['system.budget', 'system.ledgers', 'budget'],
  // → system.register
  ['system.situations', 'system.register', 'situations'],
  ['system.narratives', 'system.register', 'narratives'],
  // → system.optimizer
  ['system.optimizer.diff', 'system.optimizer', 'diff'],
  // → system.wall (the KPI strip is the header band: no tab)
  ['v4.kpi', 'system.wall', undefined],
  ['system.wall_movers', 'system.wall', 'movers'],
  // → system.provenance
  ['source.fanout', 'system.provenance', 'fanout'],
  // → the read (the index is the object rail)
  ['system.navigator', 'v4.morning_read', undefined],
]

beforeEach(() => resetPanelParams())

describe('every retired id lands on its survivor at the right tab — the executor\'s alias path', () => {
  it('covers exactly 23 ids, none of them still registered', () => {
    expect(LANDINGS).toHaveLength(23)
    expect(new Set(LANDINGS.map(([id]) => id)).size).toBe(23)
    for (const [id] of LANDINGS) expect(PANEL_REGISTRY[id as PanelKind], id).toBeUndefined()
  })

  it.each(LANDINGS)('open_panel(%s) → openSingleton(%s, %s) for the operator', (id, survivor, tab) => {
    const sh = fakeShell()
    const out = applyAction(act('open_panel', { kind: id, params: {} }), sh, { driver: 'operator' })
    expect(out).toEqual({ state: 'executed' })
    expect(sh.openSingleton).toHaveBeenCalledTimes(1)
    expect(sh.openSingleton).toHaveBeenCalledWith(survivor, tab)
    // The alias table says the same thing the executor did.
    expect(KIND_ALIASES[id]).toEqual(tab ? { kind: survivor, tab } : { kind: survivor })
  })

  it.each(LANDINGS)('the panel_id of %s resolves onto the same survivor and tab', (id, survivor, tab) => {
    expect(resolveRetiredPanelId(id.replace(/[.]/g, '_'))).toEqual(tab ? { kind: survivor, tab } : { kind: survivor })
  })

  it('a recorded CASE naming a drive-pack id that retired still opens its survivor', () => {
    // The drive pack's allowlist lists ids as recorded; `v4.kpi` and
    // `system.navigator` were drivable before they retired.
    for (const [id, survivor, tab] of LANDINGS) {
      if (!isDrivableKind(id, CASE_VOCABULARY)) continue
      const sh = fakeShell()
      expect(applyAction(act('open_panel', { kind: id, params: {} }), sh), id).toEqual({ state: 'executed' })
      expect(sh.openSingleton).toHaveBeenCalledWith(survivor, tab)
    }
  })

  it('an operator\'s explicit tab on the survivor still wins over the alias tab', () => {
    const sh = fakeShell()
    applyAction(act('open_panel', { kind: 'system.dead_letter', params: { tab: 'queues' } }), sh, { driver: 'operator' })
    expect(sh.openSingleton).toHaveBeenCalledWith('system.status', 'queues')
  })

  it('the optimizer diff drill: the candidate rides the SURVIVOR\'s params, opened on Diff', () => {
    const sh = fakeShell()
    applyAction(
      act('open_panel', { kind: 'system.optimizer.diff', params: { candidate_id: 'c-7' } }),
      sh,
      { driver: 'operator' },
    )
    expect(pendingPanelParams('system.optimizer')?.params).toEqual({ candidate_id: 'c-7' })
    expect(sh.openSingleton).toHaveBeenCalledWith('system.optimizer', 'diff')
  })

  it('mode gating holds on the survivor: a personal-only Engine survivor is skipped in cis', () => {
    const sh = fakeShell('cis')
    const out = applyAction(act('open_panel', { kind: 'system.audit', params: {} }), sh, { driver: 'operator' })
    expect(out).toMatchObject({ state: 'skipped', reason: expect.stringMatching(/^mode-gated/) })
    expect(sh.openSingleton).not.toHaveBeenCalled()
  })
})

describe('zero components deleted — every retired kind\'s component still ships, mounted by its survivor', () => {
  // Keys are the source files; the lazy import in registry.ts was the only
  // thing the retirement removed. (A git-ls-files check needs the repo's .git,
  // which the UI container does not mount; the bundle's own glob is the same
  // fact from inside the build.)
  const FILES = import.meta.glob('/src/panels/**/*.tsx')
  const RETIRED_COMPONENTS = [
    '/src/panels/registry/Targets.tsx',
    '/src/panels/registry/Analysts.tsx',
    '/src/panels/registry/Stack.tsx',
    '/src/panels/registry/ActionPacks.tsx',
    '/src/panels/source/SourceRegistry.tsx',
    '/src/panels/source/SourceDetail.tsx',
    '/src/panels/source/SubscriptionBuilder.tsx',
    '/src/panels/source/SubscriptionPolicy.tsx',
    '/src/panels/source/FanoutExplorer.tsx',
    '/src/panels/system/SystemStatus.tsx',
    '/src/panels/system/ProductionGauge.tsx',
    '/src/panels/system/DeadLetter.tsx',
    '/src/panels/system/ActorHealth.tsx',
    '/src/panels/system/StreamLag.tsx',
    '/src/panels/system/GovernorEvents.tsx',
    '/src/panels/system/AuditChain.tsx',
    '/src/panels/system/Budget.tsx',
    '/src/panels/system/Optimizer.tsx',
    '/src/panels/system/OptimizerDiff.tsx',
    '/src/panels/system/Wall.tsx',
    '/src/panels/system/WallMovers.tsx',
    '/src/panels/system/SituationTrajectory.tsx',
    '/src/panels/system/Narratives.tsx',
    // W10-F7 (W11): '/src/panels/system/Navigator.tsx' was DELETED — mounted
    // nowhere since W8 (the alias opens the read; the products index is the
    // rail's Reads section) and imported by nothing but its own test. The
    // zero-deleted rule was W8's merge rule, not a standing one.
    '/src/panels/v4/KpiPanel.tsx',
    '/src/panels/v4/FlowPanel.tsx',
  ]

  it('every one of them is still a source file', () => {
    const missing = RETIRED_COMPONENTS.filter((f) => !(f in FILES))
    expect(missing).toEqual([])
  })

  it('the six new and re-cut wrappers exist beside them', () => {
    for (const w of ['Registry', 'Status', 'Ledgers', 'Register', 'Optimizer', 'Wall', 'Provenance']) {
      expect(`/src/panels/merged/${w}.tsx` in FILES, w).toBe(true)
    }
  })
})
