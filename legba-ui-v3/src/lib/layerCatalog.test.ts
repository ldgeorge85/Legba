/**
 * The map's layer catalog (UI-B4/map): every layer declares its aperture; the
 * default layer set comes from the profile; an unanswered layer is *no read
 * yet*, never 0.
 */
import { describe, expect, it } from 'vitest'
import { profileUiFrom, PROFILE_UI } from './profileUi'
import {
  LAYER_CATALOG,
  apertureLines,
  defaultLayerIds,
  layerAvailable,
  layerCountFigure,
  layersForProfile,
  catalogLayer,
  type LayerAperture,
} from './layerCatalog'
import { CASE_VOCABULARY } from './caseModel'
import { useWorldState } from '@/v4/world/worldState'
import noneFixture from '@/test/profileNone.fixture.json'

const NONE = profileUiFrom(noneFixture)
const DECLARED = profileUiFrom({ ...JSON.parse(JSON.stringify(noneFixture)), map: { geometry: 'declared' } })

describe('every layer in the catalog has an aperture', () => {
  it.each(LAYER_CATALOG.map((l) => [l.id, l] as const))('%s declares all five aperture fields', (_id, layer) => {
    const keys: (keyof LayerAperture)[] = ['reads', 'sourceLayers', 'window', 'subjects', 'placement']
    for (const k of keys) expect(layer.aperture[k].trim().length).toBeGreaterThan(10)
    expect(apertureLines(layer).map((l) => l.label)).toEqual(['Reads', 'Source layers', 'Window', 'Subjects', 'Placement'])
    expect(layer.geometries.length).toBeGreaterThan(0)
  })

  it('ids are unique, and every layer the case vocabulary names is in the catalog', () => {
    const ids = LAYER_CATALOG.map((l) => l.id)
    expect(new Set(ids).size).toBe(ids.length)
    const storeKeys = LAYER_CATALOG.map((l) => l.storeKey).filter(Boolean)
    for (const layer of CASE_VOCABULARY.layers) expect(storeKeys).toContain(layer)
  })

  it('a layer no renderer draws says so instead of hiding it', () => {
    const entities = catalogLayer('entities')
    expect(entities.drawn).toBe(false)
    expect(entities.aperture.placement).toMatch(/not drawn/)
  })
})

describe('the default layer set comes from the profile', () => {
  it("the iso2 profile keeps today's layers (snapshot of the default ids)", () => {
    expect(defaultLayerIds(PROFILE_UI)).toEqual([
      'choropleth',
      'signals',
      'findings',
      'situations',
      'events',
      'convergence',
      'watch',
    ])
    expect(defaultLayerIds(PROFILE_UI, 'drawn')).toEqual(['signals', 'findings', 'situations', 'events'])
  })

  it("the store's initial toggles are the catalog's store defaults", () => {
    const initial = useWorldState.getState().layers
    for (const l of LAYER_CATALOG) {
      if (l.storeKey) expect(initial[l.storeKey]).toBe(l.defaultOn)
    }
  })

  it('a `none` profile has no layers at all', () => {
    expect(layersForProfile(NONE)).toEqual([])
    expect(defaultLayerIds(NONE)).toEqual([])
  })

  it('a `declared` profile is never forced onto a choropleth', () => {
    expect(layerAvailable('choropleth', DECLARED)).toBe(false)
    expect(layerAvailable('arcs', DECLARED)).toBe(false)
    expect(defaultLayerIds(DECLARED)).not.toContain('choropleth')
    expect(defaultLayerIds(DECLARED)).toContain('signals')
  })
})

describe('a layer count is a Figure', () => {
  const end = Date.parse('2026-10-04T07:00:00Z')
  it('an unanswered read is no read yet — never 0', () => {
    expect(layerCountFigure(catalogLayer('signals'), undefined, end)).toMatchObject({ state: 'no_read_yet', unit: 'in window' })
    expect(layerCountFigure(catalogLayer('signals'), null, end).state).toBe('no_read_yet')
  })

  it('a measured zero stays a zero, with its unit and as-of', () => {
    expect(layerCountFigure(catalogLayer('signals'), 0, end)).toEqual({
      state: 'measured',
      value: 0,
      unit: 'in window',
      asOf: '2026-10-04 07:00Z',
    })
  })

  it('an undrawn layer prints the dash', () => {
    expect(layerCountFigure(catalogLayer('entities'), 3, end).state).toBe('dash')
  })
})
