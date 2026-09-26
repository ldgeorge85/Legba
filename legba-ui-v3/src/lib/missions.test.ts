/**
 * The four missions, and the one rule that makes them trustworthy: a mission
 * DECLARES what it wants to be about and RESOLVES that against what the reader
 * actually has. It never invents a desk, and it never clears the wall and lets
 * the emptiness read as an answer.
 *
 * What is pinned here:
 *  - the four ship, each naming a real stance, opening real panel kinds, and
 *    carrying a window with a positive span and a unit;
 *  - `missionShows` is DERIVED from the object, so the chooser's promise and
 *    what choosing actually does cannot drift apart;
 *  - the resolution's four branches, including the two that decline to act and
 *    say why.
 */
import { describe, it, expect } from 'vitest'
import {
  MISSIONS,
  MISSION_SCOPE_ORIGIN,
  findMission,
  isMissionId,
  missionForWorkspace,
  missionPanels,
  missionShows,
  resolveMissionScope,
  type MissionDef,
} from '@/lib/missions'
import { findWorkspace } from '@/lib/workspaces'
import { PANEL_REGISTRY } from '@/panel-registry/registry'
import { emptyMembers, type Scope } from '@/state/scope'
import type { Selection } from '@/state/selection'

const byId = (id: string): MissionDef => findMission(id)!

function scopeOf(kind: Scope['kind'], id: string, label = id): Scope {
  return { kind, id, label, members: emptyMembers(), origin: 'test' }
}

function selectionOf(kind: Selection['kind'], id: string, label?: string): Selection {
  return { kind, id, label, origin: 'test' }
}

describe('the four missions', () => {
  it('ships exactly the four named for the reader\'s job', () => {
    expect(MISSIONS.map((m) => m.id)).toEqual([
      'morning_read',
      'desk_watch',
      'crisis',
      'release',
    ])
    expect(MISSIONS.map((m) => m.label)).toEqual([
      'Morning Read',
      'Desk Watch',
      'Crisis',
      'Release',
    ])
  })

  it('each names a real stance and opens only registered panel kinds', () => {
    for (const m of MISSIONS) {
      expect(findWorkspace(m.workspace), `${m.id} stance`).toBeDefined()
      for (const placement of m.panels) {
        expect(PANEL_REGISTRY[placement.kind], `${m.id} → ${placement.kind}`).toBeDefined()
        // A placement's reference panel must be a kind the stance actually
        // seeds, or the re-anchoring fallback silently swallows the layout.
        const ref = placement.position?.referencePanel
        if (ref) {
          const seeded = findWorkspace(m.workspace)!.seed.map((p) => p.kind)
          expect([...seeded, ...m.panels.map((p) => p.kind)], `${m.id} ref ${ref}`).toContain(ref)
        }
      }
    }
  })

  it('each carries a temporal window with a positive span and a unit', () => {
    for (const m of MISSIONS) {
      expect(m.timeWindow.spanMs).toBeGreaterThan(0)
      expect(m.timeWindow.label).toMatch(/^\d+[hd]$/)
    }
  })

  it('id lookups are total and reject anything else', () => {
    expect(isMissionId('crisis')).toBe(true)
    expect(isMissionId('investigate')).toBe(false)
    expect(findMission('nope')).toBeUndefined()
    expect(missionForWorkspace('trust')?.id).toBe('release')
    // The two stances about the machine carry no mission — the chooser names
    // them rather than pretending they have an aperture to declare.
    expect(missionForWorkspace('gate')).toBeUndefined()
    expect(missionForWorkspace('engine')).toBeUndefined()
  })

  it('the panel set is the stance seed PLUS the mission tiles, never double-counted', () => {
    const crisis = byId('crisis')
    const seeded = findWorkspace(crisis.workspace)!.seed.map((p) => p.kind)
    const panels = missionPanels(crisis)
    // Every seeded kind survives, in seed order, and nothing appears twice.
    expect(panels.slice(0, seeded.length)).toEqual(seeded)
    expect(new Set(panels).size).toBe(panels.length)
    // Consult is already in the investigate seed, so the mission asking for it
    // again must not add a second entry.
    expect(panels.filter((k) => k === 'system.consult')).toHaveLength(1)
    // …and the tiles that stance lacks ARE added.
    expect(panels).toContain('v4.map')
    expect(panels).toContain('system.timeline')
  })

  it('every mission ends up with the Consult tile in its panel set', () => {
    for (const m of MISSIONS) {
      expect(missionPanels(m), m.id).toContain('system.consult')
    }
  })
})

describe('missionShows — derived, so the chooser cannot promise what choosing does not do', () => {
  it('names the tile count, the scope, the window and the layers', () => {
    const line = missionShows(byId('desk_watch'))
    expect(line).toContain('tiles')
    expect(line).toContain('scoped to the desk you have selected')
    expect(line).toContain('7d window')
    expect(line).toContain('map layers: signals, findings, events')
    expect(line).toContain('Consult pinned to the scope')
  })

  it('states an empty layer claim as a claim, not as "no layers"', () => {
    const release = byId('release')
    expect(release.layers).toEqual([])
    const line = missionShows(release)
    expect(line).toContain('map layers: left as you set them')
    // "no layers" would read as "every layer off", which is a different claim.
    expect(line).not.toContain('no layers')
  })

  it('counts the same tiles the mission actually mounts', () => {
    for (const m of MISSIONS) {
      expect(missionShows(m)).toContain(`${missionPanels(m).length} tile`)
    }
  })
})

describe('resolveMissionScope — declaration → what actually happens', () => {
  it('Morning Read clears the scope: the whole roster, no desk filter', () => {
    const r = resolveMissionScope(byId('morning_read'), {
      scope: scopeOf('target', 'country_g20_ar', 'Argentina'),
      selection: null,
    })
    expect(r.action).toBe('clear')
    expect(r.scope).toBeNull()
    expect(r.note).toContain('whole roster')
  })

  it('Release leaves the wall alone and names what it left it on', () => {
    const r = resolveMissionScope(byId('release'), {
      scope: scopeOf('target', 'country_g20_ar', 'Argentina'),
      selection: null,
    })
    expect(r.action).toBe('keep')
    expect(r.note).toContain('Argentina')
    expect(r.note).toContain('about the machine')
  })

  it('Desk Watch takes the desk the reader had SELECTED', () => {
    const r = resolveMissionScope(byId('desk_watch'), {
      scope: null,
      selection: selectionOf('target', 'country_watch_il', 'Israel'),
    })
    expect(r.action).toBe('set')
    expect(r.scope).toMatchObject({
      kind: 'target',
      id: 'country_watch_il',
      label: 'Israel',
      origin: MISSION_SCOPE_ORIGIN,
    })
    // DEGRADED on purpose: members are projected from a payload this pure
    // module will not fetch, and an empty member set filters nothing.
    expect(r.scope!.members).toEqual(emptyMembers())
    expect(r.note).toContain('Israel')
  })

  it('Desk Watch with NO desk selected keeps the scope and says so — it invents nothing', () => {
    const held = scopeOf('report', 'assembly-1', 'World read')
    const r = resolveMissionScope(byId('desk_watch'), { scope: held, selection: null })
    expect(r.action).toBe('keep')
    expect(r.scope).toBeNull()
    expect(r.note).toContain('no desk selected')
    expect(r.note).toContain('Desk Watch')
  })

  it('a mission already on its own kind of scope does not re-scope', () => {
    const r = resolveMissionScope(byId('desk_watch'), {
      scope: scopeOf('target', 'country_g20_cn', 'China'),
      selection: selectionOf('target', 'country_watch_il', 'Israel'),
    })
    expect(r.action).toBe('keep')
    expect(r.note).toContain('already scoped to desk China')
  })

  it('Crisis takes the SITUATION the reader had selected, not a desk', () => {
    const r = resolveMissionScope(byId('crisis'), {
      scope: null,
      selection: selectionOf('situation', 'sit-42', 'Strait closure'),
    })
    expect(r.action).toBe('set')
    expect(r.scope).toMatchObject({ kind: 'situation', id: 'sit-42' })

    const noSituation = resolveMissionScope(byId('crisis'), {
      scope: null,
      selection: selectionOf('target', 'country_g20_ar', 'Argentina'),
    })
    expect(noSituation.action).toBe('keep')
    expect(noSituation.note).toContain('no situation selected')
  })

  it('falls back to the id when a selection carries no label', () => {
    const r = resolveMissionScope(byId('desk_watch'), {
      scope: null,
      selection: selectionOf('target', 'country_g20_br'),
    })
    expect(r.scope?.label).toBe('country_g20_br')
  })
})
