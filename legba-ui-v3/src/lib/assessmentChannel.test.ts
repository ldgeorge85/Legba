/**
 * assessmentChannel — WHICH voice, and WHICH of its rows, belongs to a record.
 *
 * The two failures this module exists to prevent, written as tests:
 *
 *   * a tier with no channel borrowing another tier's voice (the constant's
 *     behaviour: a thematic read fell through to the newest world assessment);
 *   * a desk borrowing its neighbour's (the fallback-to-newest branch, once the
 *     channel is per-country rather than one row a cycle).
 */
import { describe, it, expect } from 'vitest'

import {
  ASSESSMENT_CHANNELS,
  assessmentChannelFor,
  COUNTRY_SPINE_ANALYST,
  isAssessmentAnalyst,
  pickAssessmentRow,
  spineAnalystFor,
  WORLD_SPINE_ANALYST,
} from './assessmentChannel'
import { projectAssembly, type AssemblyPayload, type ReadFindingRow } from './assemblyModel'
import worldRow from '@/v4/read/__fixtures__/assembly.world.json'
import countryRow from '@/v4/read/__fixtures__/assembly.country.json'
import thematicRow from '@/v4/read/__fixtures__/assembly.thematic.json'
import countryAssessmentRow from '@/v4/read/__fixtures__/assessment.country.json'

const WORLD = worldRow as unknown as ReadFindingRow
const COUNTRY = countryRow as unknown as ReadFindingRow
const THEMATIC = thematicRow as unknown as ReadFindingRow
const COUNTRY_VOICE = countryAssessmentRow as unknown as ReadFindingRow

const worldAssembly = projectAssembly(WORLD)!
const countryAssembly = projectAssembly(COUNTRY)!

describe('assessmentChannelFor — the voice is a function of the record', () => {
  it('gives the world spine the world channel', () => {
    const c = assessmentChannelFor(WORLD, worldAssembly)
    expect(c?.analystId).toBe('world_assessment')
    expect(c?.targetScoped).toBe(false)
    expect(c?.label).toBe('Assessment')
  })

  it('gives a country assembly the country channel, scoped to its desk', () => {
    const c = assessmentChannelFor(COUNTRY, countryAssembly)
    expect(c?.analystId).toBe('country_assessment')
    expect(c?.tier).toBe('country')
    expect(c?.targetScoped).toBe(true)
    expect(c?.label).toBe('Country assessment')
  })

  it('gives a thematic or region read NO channel — none exists at that tier', () => {
    expect(assessmentChannelFor(THEMATIC, projectAssembly(THEMATIC))).toBeNull()
    expect(
      assessmentChannelFor({ ...COUNTRY, analyst_id: 'region_composition' }, countryAssembly),
    ).toBeNull()
  })

  it('refuses a channel when the producer and the payload disagree about the tier', () => {
    // A country producer whose payload claims the world tier cannot be fenced to
    // either voice: the honest answer is no band, not a guess.
    const mislabelled: AssemblyPayload = { ...countryAssembly, tier: 'world' }
    expect(assessmentChannelFor(COUNTRY, mislabelled)).toBeNull()
  })

  it('refuses a row with no assembly at all (a legacy prose run)', () => {
    expect(assessmentChannelFor(WORLD, null)).toBeNull()
    expect(assessmentChannelFor(null, worldAssembly)).toBeNull()
  })

  it('knows a channel producer from a spine producer', () => {
    expect(isAssessmentAnalyst('world_assessment')).toBe(true)
    expect(isAssessmentAnalyst('country_assessment')).toBe(true)
    expect(isAssessmentAnalyst(WORLD_SPINE_ANALYST)).toBe(false)
    expect(isAssessmentAnalyst(COUNTRY_SPINE_ANALYST)).toBe(false)
    expect(isAssessmentAnalyst(null)).toBe(false)
  })
})

describe('spineAnalystFor — a scoped voice resolves to the record it grades', () => {
  it('maps each channel back to its spine, and nothing else', () => {
    expect(spineAnalystFor('country_assessment')).toBe(COUNTRY_SPINE_ANALYST)
    expect(spineAnalystFor('world_assessment')).toBe(WORLD_SPINE_ANALYST)
    expect(spineAnalystFor('country_composition')).toBeNull()
    expect(spineAnalystFor(null)).toBeNull()
  })
})

describe('pickAssessmentRow — fenced to the record, or stated as not', () => {
  const channel = ASSESSMENT_CHANNELS[COUNTRY_SPINE_ANALYST]

  it('prefers the row whose derived_from names this very record', () => {
    const older = {
      ...COUNTRY_VOICE,
      id: 'older-voice',
      derived_from: ['some-earlier-country-run'],
      produced_at: '2026-09-04T00:00:00Z', // newer, and still not the fenced one
    }
    expect(pickAssessmentRow([older, COUNTRY_VOICE], channel, COUNTRY)?.id).toBe(COUNTRY_VOICE.id)
  })

  it('falls back to the newest row for the SAME desk when none is fenced here', () => {
    const other = {
      ...COUNTRY_VOICE,
      id: 'older-voice',
      derived_from: ['some-earlier-country-run'],
      produced_at: '2026-09-02T11:52:00Z',
    }
    const newer = { ...other, id: 'newer-voice', produced_at: '2026-09-03T11:52:00Z' }
    expect(pickAssessmentRow([other, newer], channel, COUNTRY)?.id).toBe('newer-voice')
  })

  it('never crosses desks — a neighbour’s voice is an absence, not a fallback', () => {
    const neighbour = {
      ...COUNTRY_VOICE,
      id: 'israel-voice',
      target_id: 'country_watch_il',
      derived_from: ['some-israel-run'],
    }
    expect(pickAssessmentRow([neighbour], channel, COUNTRY)).toBeNull()
  })

  it('ignores rows written by another producer, and an empty channel', () => {
    const notTheChannel = { ...COUNTRY_VOICE, analyst_id: 'world_assessment' }
    expect(pickAssessmentRow([notTheChannel], channel, COUNTRY)).toBeNull()
    expect(pickAssessmentRow([], channel, COUNTRY)).toBeNull()
    expect(pickAssessmentRow([COUNTRY_VOICE], null, COUNTRY)).toBeNull()
    expect(pickAssessmentRow([COUNTRY_VOICE], channel, null)).toBeNull()
  })
})
