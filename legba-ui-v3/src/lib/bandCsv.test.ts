/**
 * bandCsv (7b-iii, lane k1) — the Eval Scorecard band-series CSV.
 *
 * Asserts the contract the panel's own honesty rules impose on the file:
 * RFC 4180 quoting, an EMPTY cell for every absent value (never `0`, never
 * "null"), a real `0` only where the card records an empty basis, one row per
 * (desk, dimension) with the insufficient dimensions still present, the
 * panel's own stable ordering, and the `<desk-or-all>_<asof>` filename —
 * including the `undated` case, where no exported row carries an as-of.
 */
import { describe, it, expect } from 'vitest'
import {
  BAND_CSV_COLUMNS,
  bandCsvFilename,
  bandCsvRows,
  buildBandCsv,
  csvField,
  toCsv,
} from './bandCsv'
import type { CountryScorecard, DimensionBand } from './evalOps'

function dim(over: Partial<DimensionBand> = {}): DimensionBand {
  return {
    band: 'watch',
    basis: ['f1', 'f2'],
    severity_tag: null,
    effective_confidence: 0.6,
    confidence: 0.7,
    critic_score: 0.8,
    damped: false,
    reason: 'banded',
    produced_at: '2026-09-20T04:00:00Z',
    eval: {
      faithfulness: null,
      correctness_vs_reference: null,
      n_labeled: 0,
      faithfulness_flagged: false,
    },
    ...over,
  }
}

function card(over: Partial<CountryScorecard> = {}): CountryScorecard {
  return {
    target_id: 'country_g20_tr',
    id: 'sc-1',
    produced_at: '2026-09-21T00:00:00Z',
    generated_at: '2026-09-21T06:30:00Z',
    floors: {},
    dimensions: { escalation: dim() },
    composition: { present: true, basis: ['c1'] },
    method_version: 'band.v3',
    ...over,
  }
}

describe('csvField', () => {
  it('leaves a plain value unquoted', () => {
    expect(csvField('watch')).toBe('watch')
    expect(csvField(0)).toBe('0')
  })

  it('renders an absent value as an EMPTY cell, never 0 and never "null"', () => {
    expect(csvField(null)).toBe('')
    expect(csvField(undefined)).toBe('')
  })

  it('quotes and doubles quotes, commas, newlines and edge whitespace', () => {
    expect(csvField('a,b')).toBe('"a,b"')
    expect(csvField('say "hi"')).toBe('"say ""hi"""')
    expect(csvField('line\nbreak')).toBe('"line\nbreak"')
    expect(csvField(' padded ')).toBe('" padded "')
  })
})

describe('toCsv', () => {
  it('writes a header row and CRLF terminators', () => {
    const out = toCsv(['a', 'b'], [['1', '2'], ['3', null]])
    expect(out).toBe('a,b\r\n1,2\r\n3,\r\n')
  })
})

describe('bandCsvRows', () => {
  it('emits one row per (desk, dimension), insufficient dimensions included', () => {
    const rows = bandCsvRows([
      card({
        dimensions: {
          escalation: dim({ band: 'high' }),
          energy_security: dim({ band: 'insufficient-evidence', basis: [] }),
        },
      }),
    ])
    expect(rows).toHaveLength(2)
    expect(rows.map((r) => r.dimension)).toEqual(['energy_security', 'escalation'])
    expect(rows.map((r) => r.band)).toEqual(['insufficient-evidence', 'high'])
  })

  it('counts the real basis — 0 only where the card records an empty basis', () => {
    const rows = bandCsvRows([
      card({
        dimensions: {
          escalation: dim({ basis: ['f1', 'f2', 'f3'] }),
          energy_security: dim({ band: 'insufficient-evidence', basis: [] }),
        },
      }),
    ])
    const byDim = Object.fromEntries(rows.map((r) => [r.dimension, r.verified_basis_count]))
    expect(byDim.escalation).toBe(3)
    expect(byDim.energy_security).toBe(0)
  })

  it('keeps the two as-ofs apart and never substitutes one for the other', () => {
    const [row] = bandCsvRows([
      card({
        generated_at: '2026-09-21T06:30:00Z',
        dimensions: { escalation: dim({ produced_at: null }) },
      }),
    ])
    expect(row.band_produced_at).toBeNull()
    expect(row.card_generated_at).toBe('2026-09-21T06:30:00Z')
  })

  it('leaves method_version absent on a pre-H12 card rather than guessing', () => {
    const [row] = bandCsvRows([card({ method_version: null })])
    expect(row.method_version).toBeNull()
  })

  it('orders desks by id and dimensions in the panel display order', () => {
    const rows = bandCsvRows([
      card({
        target_id: 'country_g20_us',
        dimensions: { zz_extra: dim(), escalation: dim(), leadership_transition: dim() },
      }),
      card({ target_id: 'country_g20_br', dimensions: { escalation: dim() } }),
    ])
    expect(rows.map((r) => `${r.desk_id}/${r.dimension}`)).toEqual([
      'country_g20_br/escalation',
      'country_g20_us/leadership_transition',
      'country_g20_us/escalation',
      'country_g20_us/zz_extra',
    ])
  })

  it('resolves the desk name beside the raw desk id', () => {
    const [row] = bandCsvRows([card({ target_id: 'country_g20_tr' })])
    expect(row.desk_id).toBe('country_g20_tr')
    expect(row.desk).not.toBe('')
    expect(row.desk).not.toBe('country_g20_tr')
  })
})

describe('bandCsvFilename', () => {
  it('names the single desk it covers, stamped with the newest as-of', () => {
    const rows = bandCsvRows([card({ target_id: 'country_g20_tr' })])
    expect(bandCsvFilename(rows)).toBe('legba_bands_country_g20_tr_20260921.csv')
  })

  it('says "all" across more than one desk', () => {
    const rows = bandCsvRows([
      card({ target_id: 'country_g20_tr' }),
      card({ target_id: 'country_g20_us' }),
    ])
    expect(bandCsvFilename(rows)).toBe('legba_bands_all_20260921.csv')
  })

  it('says "undated" rather than stamping today when no row carries an as-of', () => {
    const rows = bandCsvRows([
      card({ generated_at: null, dimensions: { escalation: dim({ produced_at: null }) } }),
    ])
    expect(bandCsvFilename(rows)).toBe('legba_bands_country_g20_tr_undated.csv')
  })
})

describe('buildBandCsv', () => {
  it('writes the declared header and one line per row, absent cells empty', () => {
    const { filename, content } = buildBandCsv([
      card({
        target_id: 'country_g20_tr',
        method_version: null,
        dimensions: {
          escalation: dim({ band: 'high', basis: ['f1'], produced_at: null }),
        },
      }),
    ])
    const lines = content.split('\r\n')
    expect(lines[0]).toBe(BAND_CSV_COLUMNS.join(','))
    expect(lines[1]).toBe(
      'country_g20_tr,Turkey,escalation,high,1,,2026-09-21T06:30:00Z,',
    )
    // trailing terminator, no stray row
    expect(lines[2]).toBe('')
    expect(filename).toBe('legba_bands_country_g20_tr_20260921.csv')
  })

  it('is stable — the same cards serialise byte-identically', () => {
    const cards = [card({ target_id: 'country_g20_us' }), card({ target_id: 'country_g20_br' })]
    expect(buildBandCsv(cards).content).toBe(buildBandCsv(cards).content)
  })
})
