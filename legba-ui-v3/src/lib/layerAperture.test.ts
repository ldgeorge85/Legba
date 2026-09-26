/**
 * The aperture declaration as the map's layer `i` reads it.
 *
 * The declaration exists so that a structurally missing layer never reads as
 * agreement, and it only does that job if the `i` keeps four things apart:
 * declared absent with nothing ingested; declared absent while rows ARE
 * arriving (a curation judgement, and the louder statement of the two);
 * unmeasured, which is nobody having looked; and undeclared, which is the
 * loaded map being incomplete. A desk with no map at all, and a receipt that
 * could not be read, are two further answers and neither is six blanks.
 */
import { describe, it, expect } from 'vitest'
import {
  LAYER_VOCAB,
  apertureExplainer,
  apertureSummary,
  deskAperture,
} from '@/lib/layerAperture'
import type { LayerDivergenceResponse } from '@/lib/api'

/** A receipt shaped like the live one, cut down to what the `i` reads. */
function receipt(over: Partial<LayerDivergenceResponse> = {}): LayerDivergenceResponse {
  return {
    measured: true,
    generated_at: '2026-09-26T16:13:41.035612Z',
    reader_version: '2026-09/p6-l2',
    unit_sentence: '',
    as_of: '2026-09-26',
    receipt_run_id: 'run-1',
    run_started_at: null,
    method_version: 'layer_divergence/2026-09.1',
    payload_schema: null,
    classification_audit: null,
    window_days: 30,
    baseline_days: 14,
    z_threshold: 2,
    mad_floor: null,
    consecutive_days: 2,
    thin_min_per_day: 3,
    layer_vocab: [...LAYER_VOCAB],
    pairs_declared: [],
    desks: [
      {
        target_id: 'country_g20_ar',
        country: 'AR',
        map_version: 'layer_map_ar.v1',
        sources_mapped: 49,
        rows_scanned: 100,
        rows_truncated: false,
        aperture: {},
        counts_suppressed_by_aperture: { social_digest: 10 },
        layers: {
          official: {
            declared: 'absent',
            reason: 'curated: no Argentine government feed is registered',
            sources_mapped: 0,
            thin_days: null,
            daily: [],
          },
          domestic_press: {
            declared: 'present',
            reason: '',
            sources_mapped: 12,
            thin_days: 3,
            daily: [],
          },
          foreign_press: {
            declared: 'present',
            reason: '',
            sources_mapped: 30,
            thin_days: 0,
            daily: [],
          },
          social_digest: {
            declared: 'absent',
            reason: 'curated: the Telegram list carries no Argentine channels',
            sources_mapped: 2,
            thin_days: null,
            daily: [],
          },
          public_data: {
            declared: 'unmeasured',
            reason: '',
            sources_mapped: 5,
            thin_days: null,
            daily: [],
          },
          // `physical` is deliberately MISSING — the undeclared branch.
        },
        pairs: [],
        fired: null,
      },
    ],
    desks_unresolved: [],
    warnings: [],
    ...over,
  } as LayerDivergenceResponse
}

const rowFor = (desk: ReturnType<typeof deskAperture>, layer: string) =>
  desk.rows.find((r) => r.layer === layer)!

describe('deskAperture — folding the receipt to one desk', () => {
  it('resolves by target id and carries the map version and the run as-of', () => {
    const desk = deskAperture(receipt(), { targetId: 'country_g20_ar' })
    expect(desk.note).toBeNull()
    expect(desk.country).toBe('AR')
    expect(desk.mapVersion).toBe('layer_map_ar.v1')
    expect(desk.asOf).toBe('2026-09-26')
    expect(desk.methodVersion).toBe('layer_divergence/2026-09.1')
    expect(desk.rows.map((r) => r.layer)).toEqual([...LAYER_VOCAB])
  })

  it('resolves by ISO2 too — the map has its own country filter', () => {
    expect(deskAperture(receipt(), { iso2: 'ar' }).targetId).toBe('country_g20_ar')
  })

  it('keeps the four declarations apart, and the suppressed count with them', () => {
    const desk = deskAperture(receipt(), { targetId: 'country_g20_ar' })
    expect(rowFor(desk, 'domestic_press').declared).toBe('present')
    expect(rowFor(desk, 'official').declared).toBe('absent')
    expect(rowFor(desk, 'official').sourcesMapped).toBe(0)
    expect(rowFor(desk, 'social_digest').declared).toBe('absent')
    expect(rowFor(desk, 'social_digest').suppressed).toBe(10)
    expect(rowFor(desk, 'public_data').declared).toBe('unmeasured')
    // A layer the loaded map carries no row for is UNDECLARED, never silently
    // unmeasured — those are different statements.
    expect(rowFor(desk, 'physical').declared).toBe('undeclared')
  })

  it('a desk with no loaded map says so, and names the maps that ARE loaded', () => {
    const desk = deskAperture(receipt(), { targetId: 'country_g20_br' })
    expect(desk.rows.every((r) => r.declared === 'no_map')).toBe(true)
    expect(desk.note).toContain('no layer map is loaded for this desk')
    expect(desk.note).toContain('AR')
  })

  it('no country in scope is its own answer, not an empty table', () => {
    const desk = deskAperture(receipt(), {})
    expect(desk.note).toContain('no country is in scope')
    expect(desk.rows).toHaveLength(LAYER_VOCAB.length)
  })

  it('a failed read is a failed read, not a quiet aperture', () => {
    expect(deskAperture(undefined, { targetId: 'x' }, true).note).toContain('failed read')
    expect(
      deskAperture(receipt({ measured: false }), { targetId: 'country_g20_ar' }).note,
    ).toContain('is not being reported')
    expect(deskAperture(undefined, { targetId: 'x' }, true).rows[0].declared).toBe('unread')
  })
})

describe('apertureExplainer — the `i`', () => {
  const desk = deskAperture(receipt(), { targetId: 'country_g20_ar' })

  it('declared absent with nothing ingested says NOT INGESTED and carries the reason', () => {
    const text = apertureExplainer(rowFor(desk, 'official'), desk)
    expect(text).toContain('declared ABSENT')
    expect(text).toContain('nothing is ingested on it')
    expect(text).toContain('no Argentine government feed is registered')
    expect(text).toContain('never reads as agreement')
    expect(text).toContain('layer_map_ar.v1')
    expect(text).toContain('as of 2026-09-26')
  })

  it('declared absent WHILE rows arrive says so — the louder statement', () => {
    const text = apertureExplainer(rowFor(desk, 'social_digest'), desk)
    expect(text).toContain('even though rows ARE arriving')
    expect(text).toContain('10 rows on this layer are suppressed')
    expect(text).toContain('curation judgement')
  })

  it('unmeasured is the honest default, explicitly not a claim of absence', () => {
    const text = apertureExplainer(rowFor(desk, 'public_data'), desk)
    expect(text).toContain('UNMEASURED')
    expect(text).toContain('nobody has looked')
    expect(text).toContain('not a claim that the layer is missing')
  })

  it('undeclared blames the map, not the layer', () => {
    const text = apertureExplainer(rowFor(desk, 'physical'), desk)
    expect(text).toContain('UNDECLARED')
    expect(text).toContain('map being incomplete')
  })

  it('present carries its source count and its thin days', () => {
    const text = apertureExplainer(rowFor(desk, 'domestic_press'), desk)
    expect(text).toContain('declared PRESENT')
    expect(text).toContain('12 sources')
    expect(text).toContain('thin on 3 days')
  })

  it('an absent layer with NO recorded reason is called out as a defect', () => {
    const r = receipt()
    // The table forbids this; the `i` must not paper over it if it happens.
    ;(r.desks[0].layers.official as { reason: string }).reason = ''
    const d = deskAperture(r, { targetId: 'country_g20_ar' })
    expect(apertureExplainer(rowFor(d, 'official'), d)).toContain('read this as a defect')
  })
})

describe('apertureSummary', () => {
  it('counts the states over the declared vocabulary', () => {
    const desk = deskAperture(receipt(), { targetId: 'country_g20_ar' })
    expect(apertureSummary(desk)).toBe(
      'AR: 2 of 6 present · 2 declared absent · 1 unmeasured · 1 undeclared',
    )
  })

  it('surfaces the note instead of a count when there is nothing to count', () => {
    expect(apertureSummary(deskAperture(receipt(), {}))).toContain('no country is in scope')
  })
})
