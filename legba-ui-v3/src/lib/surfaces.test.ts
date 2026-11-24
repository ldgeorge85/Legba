/**
 * THE ARRANGEMENT RATCHETS (U-2 shell, re-cut by the U-6 frame; 02_UI_TARGET_
 * BRIEF §6 rules 4, 5, 9, §7's cut table and §12). They replaced the workspace
 * / mission / preset tests with LOWER counts, and they may only go down:
 *
 *   - named arrangements: 17 (6 workspaces + 4 missions + 7 presets), plus
 *     user missions and the Save/Restore slot → 8 pages, nothing else;
 *   - SEEDED panel regions per page ≤ 2 (was ≤ 3 regions + a fixed Consult
 *     column); the reader may add up to 3; the stage and the chat are frame;
 *   - panel kinds reachable from the reader side ≤ 9 (was: the whole catalog,
 *     33 kinds, one click away from the landing);
 *   - reader navigation rows on the top bar = 4.
 *
 * And the model's rules: every view is registered or a declared frame view; an
 * open goes to the stage or its home region; a stored arrangement is used only
 * when it is current, complete (or closed by the reader) and within the
 * budget; every retired id lands on a page.
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { SerializedDockview } from 'dockview-react'
import { PANEL_REGISTRY } from '@/panel-registry/registry'
import { catalogKinds } from '@/panel-registry/kindPlan'
import {
  LANDING_SURFACE,
  MAX_REGIONS,
  OPERATOR_SURFACES,
  READER_SURFACES,
  RETIRED_LAYOUT_KEYS,
  SLOTS,
  SURFACES,
  SURFACE_STORE_KEY,
  homeOf,
  isFrameView,
  readSurfaceLayout,
  readerKinds,
  MAX_SEEDED_REGIONS,
  pageViews,
  resolveSurfaceId,
  saveSurfaceLayout,
  surfaceLayoutVersion,
  surfaceViews,
  surfacesForProfile,
  findSurface,
} from './surfaces'
import { LEGACY_WORKSPACES } from './workspaces'
import { LEGACY_PRESETS } from './layoutPresets'
import { LEGACY_MISSIONS } from './missions'
import { addressFor, parseAddress } from './address'

/** The ceilings. Lower them in the commit that cuts; never raise them. */
const NAMED_ARRANGEMENTS_CEILING = 8 // was 17 (+ user missions + the Save/Restore slot)
const READER_KINDS_CEILING = 9 // was every catalog kind: 33
const READER_NAV_ROWS = 4

describe('RATCHET — named arrangements: 17 → 8', () => {
  it('the console has exactly the surfaces, and nothing else is an arrangement', () => {
    expect(SURFACES.length).toBeLessThanOrEqual(NAMED_ARRANGEMENTS_CEILING)
    expect(SURFACES.map((s) => s.id)).toEqual([
      'board',
      'subject',
      'ledger',
      'watch',
      'engine',
      'evaluation',
      'gate',
      'investigate',
    ])
    // The arrangement machinery's modules export no arrangement any more.
    const files = import.meta.glob(['/src/lib/workspaces.ts', '/src/lib/layoutPresets.ts', '/src/lib/missions.ts'], {
      query: '?raw',
      import: 'default',
      eager: true,
    }) as Record<string, string>
    for (const [path, text] of Object.entries(files)) {
      expect(text, path).not.toMatch(/api\.clear\(|addPanel\(|localStorage/)
    }
  })

  it(`reader navigation rows: ${READER_NAV_ROWS}; operator sections live behind one separate entry`, () => {
    expect(READER_SURFACES.map((s) => s.id)).toEqual(['board', 'subject', 'ledger', 'watch'])
    expect(OPERATOR_SURFACES.every((s) => s.side === 'operator')).toBe(true)
    expect(LANDING_SURFACE).toBe('board')
  })

  it('keys: Alt+1–4 the reader side, Alt+5–8 the operator side, each once', () => {
    expect(SURFACES.map((s) => s.key)).toEqual([1, 2, 3, 4, 5, 6, 7, 8])
  })
})

describe('RATCHET — at most two seeded panel regions per page; the stage and Consult are frame', () => {
  for (const s of SURFACES) {
    it(`${s.id}: stage ${s.stage}, ${s.regions.length} region(s), every view registered or a declared frame view`, () => {
      expect(MAX_SEEDED_REGIONS).toBeLessThanOrEqual(2)
      expect(MAX_REGIONS).toBeLessThanOrEqual(3)
      expect(s.regions.length).toBeGreaterThan(0)
      expect(s.regions.length).toBeLessThanOrEqual(MAX_SEEDED_REGIONS)
      for (const v of pageViews(s)) {
        expect(isFrameView(v) || !!PANEL_REGISTRY[v as keyof typeof PANEL_REGISTRY], v).toBe(true)
        expect(['system.consult', 'system.inspector']).not.toContain(v)
      }
      for (const r of s.regions) {
        expect(r.views.length, `${s.id}.${r.id}`).toBeGreaterThan(0)
        expect(r.weight).toBeGreaterThan(0)
      }
      // A view lives in one place on a page: the stage or one region.
      const views = pageViews(s)
      expect(new Set(views).size).toBe(views.length)
      expect(['open', 'collapsed']).toContain(s.chat)
    })
  }

  it('the reader pages open with the chat open; the operator sections with the ask bar', () => {
    for (const s of READER_SURFACES) expect(s.chat, s.id).toBe('open')
    for (const s of OPERATOR_SURFACES) expect(s.chat, s.id).toBe('collapsed')
  })

  it("the Board's stage is today's read and its panel side leads with the map", () => {
    const board = findSurface('board')!
    expect(board.stage).toBe('v4.morning_read')
    expect(board.regions[0].views[0]).toBe('v4.map')
    expect(board.regions[0].weight).toBeGreaterThan(board.regions[1].weight)
  })
})

describe(`RATCHET — panel kinds reachable from the reader side ≤ ${READER_KINDS_CEILING}`, () => {
  it('the reader reaches its pages\' stages and panels, Consult and the Record sheet — nothing else', () => {
    const kinds = readerKinds()
    expect(kinds.length).toBeLessThanOrEqual(READER_KINDS_CEILING)
    expect(kinds.length).toBeLessThan(catalogKinds().length)
    // No engine machinery on the reader side.
    for (const k of ['system.registry', 'system.flow', 'system.ledgers', 'system.status', 'system.journal_gate', 'system.judge_stats']) {
      expect(kinds, k).not.toContain(k)
    }
  })

  it('every operator kind in the catalog has a home on the operator side or opens in Investigate', () => {
    for (const k of catalogKinds()) {
      const home = homeOf(k, 'board')
      const side = findSurface(home.surface)!.side
      if (readerKinds().includes(k)) expect(side, k).toBe('reader')
      else expect(side, k).toBe('operator')
    }
  })
})

describe('an open goes to the STAGE or its HOME region', () => {
  it("a page's own views stay where the page puts them", () => {
    expect(homeOf('v4.morning_read', 'board')).toEqual({ surface: 'board', place: 'stage', seeded: true })
    expect(homeOf('system.wall', 'board')).toEqual({ surface: 'board', place: 'b', seeded: true })
    expect(homeOf('v4.map', 'subject')).toEqual({ surface: 'subject', place: 'a', seeded: true })
  })

  it('a page-sized reader view lands on the stage of the page you are on (with a way back)', () => {
    expect(homeOf('subject.page', 'board')).toEqual({ surface: 'board', place: 'stage', seeded: false })
    expect(homeOf('view:forecasts', 'watch')).toEqual({ surface: 'watch', place: 'stage', seeded: false })
  })

  it("a panel-sized reader view swaps into this page's open region (a swap, not a trip)", () => {
    expect(homeOf('system.source_health', 'board')).toEqual({ surface: 'board', place: 'b', seeded: false })
  })

  it('an operator view crosses to the operator section that shows it', () => {
    expect(homeOf('system.registry', 'board')).toEqual({ surface: 'engine', place: 'a', seeded: true })
    expect(homeOf('system.journal', 'watch')).toEqual({ surface: 'gate', place: 'b', seeded: true })
    expect(homeOf('system.status', 'ledger')).toEqual({ surface: 'engine', place: 'stage', seeded: true })
  })

  it('on the operator side, anything opens on the section you are on', () => {
    expect(homeOf('analyst.runs', 'gate')).toEqual({ surface: 'gate', place: 'a', seeded: false })
    expect(homeOf('subject.page', 'engine')).toEqual({ surface: 'engine', place: 'stage', seeded: false })
  })

  it('a kind no section shows opens in Investigate', () => {
    expect(homeOf('analysis.cross_framing', 'subject')).toEqual({ surface: 'investigate', place: 'a', seeded: false })
  })
})

describe('the retired ids land on the nearest surface', () => {
  it('every workspace, preset and mission id resolves', () => {
    for (const w of LEGACY_WORKSPACES) expect(resolveSurfaceId(w.id), w.id).toBe(w.surface)
    for (const p of LEGACY_PRESETS) expect(resolveSurfaceId(p.id), p.id).toBe(p.surface)
    for (const m of LEGACY_MISSIONS) expect(resolveSurfaceId(m.id), m.id).not.toBeNull()
    expect(resolveSurfaceId('desk')).toBe('subject')
    expect(resolveSurfaceId('morning_read')).toBe('board')
    expect(resolveSurfaceId('release')).toBe('ledger')
    expect(resolveSurfaceId('nowhere')).toBeNull()
  })

  it('a saved view is a link: #ws, #scope, #sel and #win round-trip; arrangement is never in it', () => {
    const NOW = Date.parse('2026-11-13T12:00:00Z')
    const parsed = parseAddress('#ws=ledger&scope=target:t1&sel=finding:f1&win=7d', '', NOW)
    expect(parsed.actions.map((a) => a.action)).toEqual(['arrange', 'set_scope', 'focus', 'timeline'])
    expect(addressFor(parsed.actions)).toBe('#ws=ledger&scope=target%3At1&sel=finding%3Af1&win=7d')
    // A retired id still parses (it resolves on the shell).
    expect(parseAddress('#ws=desk').actions[0].args).toEqual({ layout: 'desk', layout_type: 'stance' })
    expect(parseAddress('#ws=nowhere').actions).toEqual([])
    expect(parseAddress('#win=11d').actions).toEqual([])
  })
})

describe('the stored arrangement: versioned, complete, within budget — or discarded', () => {
  const leaf = (views: string[], id: string) => ({ type: 'leaf', data: { views, activeView: views[0], id }, size: 400 })
  const panel = (id: string) => ({ id, contentComponent: 'default', title: id, params: { singletonKind: id } })
  const slotPanel = (id: string) => ({ id, contentComponent: 'slot', title: id, params: { slot: id } })
  const boardLayout = (extraLeaves: unknown[] = [], extraPanels: string[] = []) =>
    ({
      grid: {
        root: {
          type: 'branch',
          data: [leaf(['v4.map'], '1'), leaf(['system.wall', 'slot:coverage'], '2'), ...extraLeaves],
          size: 600,
        },
        width: 1200,
        height: 600,
        orientation: 'HORIZONTAL',
      },
      panels: {
        'v4.map': panel('v4.map'),
        'system.wall': panel('system.wall'),
        'slot:coverage': slotPanel('slot:coverage'),
        ...Object.fromEntries(extraPanels.map((id) => [id, panel(id)])),
      },
      activeGroup: '1',
    }) as unknown as SerializedDockview

  beforeEach(() => localStorage.clear())
  afterEach(() => localStorage.clear())

  it('a current, complete layout is used — slots and all', () => {
    saveSurfaceLayout('board', 'personal', boardLayout())
    const r = readSurfaceLayout('board', 'personal')
    expect(r.ok).toBe(true)
    if (r.ok) expect(Object.keys((r.layout as { panels: object }).panels)).toContain('slot:coverage')
  })

  it('an older version is discarded with a notice', () => {
    localStorage.setItem(SURFACE_STORE_KEY, JSON.stringify({ board: { personal: { version: 's1-old', layout: boardLayout() } } }))
    expect(readSurfaceLayout('board', 'personal')).toMatchObject({ ok: false, reason: 'stale' })
    expect(JSON.parse(localStorage.getItem(SURFACE_STORE_KEY)!).board).toBeUndefined()
  })

  it('a layout missing a seeded view is discarded — nothing the default shows stays hidden', () => {
    const l = boardLayout() as unknown as { panels: Record<string, unknown> }
    delete l.panels['system.wall']
    saveSurfaceLayout('board', 'personal', l as unknown as SerializedDockview)
    expect(readSurfaceLayout('board', 'personal')).toMatchObject({ ok: false, reason: 'missing_view' })
  })

  it('a seeded view the READER closed stays closed; the layout is kept', () => {
    const l = boardLayout() as unknown as { panels: Record<string, unknown> }
    delete l.panels['system.wall']
    saveSurfaceLayout('board', 'personal', l as unknown as SerializedDockview, ['system.wall'])
    const r = readSurfaceLayout('board', 'personal')
    expect(r.ok).toBe(true)
    if (r.ok) expect(r.closed).toEqual(['system.wall'])
  })

  it('the previous shell\'s slots (s1-, three regions and a fixed Consult column) are stale by construction', () => {
    const old = {
      grid: { root: { type: 'branch', data: [leaf(['v4.map'], '1'), leaf(['v4.morning_read', 'system.wall', 'slot:coverage'], '2')] }, width: 1, height: 1, orientation: 'HORIZONTAL' },
      panels: { 'v4.map': panel('v4.map'), 'v4.morning_read': panel('v4.morning_read'), 'system.wall': panel('system.wall') },
    }
    for (const id of SURFACES.map((x) => x.id)) {
      localStorage.setItem(SURFACE_STORE_KEY, JSON.stringify({ [id]: { personal: { version: 's1-1a2b3c4d', layout: old } } }))
      expect(readSurfaceLayout(id, 'personal'), id).toMatchObject({ ok: false, reason: 'stale' })
    }
  })

  it('a layout over the region budget is discarded', () => {
    saveSurfaceLayout('board', 'personal', boardLayout([leaf(['system.search'], '3'), leaf(['system.timeline'], '4')], ['system.search', 'system.timeline']))
    const l = readSurfaceLayout('board', 'personal')
    expect(l).toMatchObject({ ok: false, reason: 'too_many_regions' })
  })

  it('the retired keys are never the store the shell reads', () => {
    expect(RETIRED_LAYOUT_KEYS).not.toContain(SURFACE_STORE_KEY)
    localStorage.setItem('legba_ws', JSON.stringify({ morning_read: { personal: { layout: boardLayout() } } }))
    expect(readSurfaceLayout('board', 'personal')).toEqual({ ok: false, reason: 'none' })
  })

  it('the version is derived from the regions, so a changed default changes it', () => {
    const board = findSurface('board')!
    const v = surfaceLayoutVersion(board)
    expect(v).toMatch(/^s2-[0-9a-f]{8}$/)
    const changed = { ...board, regions: [board.regions[0]] }
    expect(surfaceLayoutVersion(changed)).not.toBe(v)
  })
})

describe('a profile with no geography', () => {
  it('drops the map and fills an emptied region with the subject field, never a blank', () => {
    const none = surfacesForProfile(SURFACES, false)
    for (const s of none) {
      expect(surfaceViews(s)).not.toContain('v4.map')
      for (const r of s.regions) expect(r.views.length).toBeGreaterThan(0)
    }
    expect(none.find((s) => s.id === 'board')!.regions[0].views).toEqual(['slot:subject_field'])
    expect(none.find((s) => s.id === 'board')!.stage).toBe('v4.morning_read')
  })

  it('every slot says what it is and what it waits on', () => {
    for (const [id, def] of Object.entries(SLOTS)) {
      expect(def.title, id).toBeTruthy()
      expect(def.what.length, id).toBeGreaterThan(20)
      expect(def.waits.length, id).toBeGreaterThan(5)
    }
  })
})
