import { describe, expect, it } from 'vitest'
import {
  activeConvergenceMarkers,
  isConvergenceChannel,
  parseConvergenceAlert,
  placeBinLabel,
  type SinceAlertRow,
} from './convergence'

function row(partial: Partial<SinceAlertRow>): SinceAlertRow {
  return {
    id: 'a1',
    severity: 'medium',
    channel: 'geo_convergence',
    summary: '',
    target_id: null,
    produced_at: '2026-07-27T00:00:00Z',
    ...partial,
  }
}

describe('convergence — channel test', () => {
  it('matches the trigger class and the analyst-id fallback', () => {
    expect(isConvergenceChannel('geo_convergence')).toBe(true)
    expect(isConvergenceChannel('geo_convergence_scan')).toBe(true)
    expect(isConvergenceChannel('band_crossing')).toBe(false)
    expect(isConvergenceChannel(null)).toBe(false)
  })
})

describe('convergence — placeBinLabel', () => {
  it('places a cell at the centre of its 1° extent', () => {
    const p = placeBinLabel('cell(33..34°, 44..45°) IQ')
    expect(p).toEqual({
      binKind: 'cell',
      binKey: 'cell:33:44',
      iso2: 'IQ',
      lat: 33.5,
      lon: 44.5,
    })
  })

  it('handles negative-coordinate cells and a missing ISO2', () => {
    const p = placeBinLabel('cell(-5..-4°, -60..-59°)')
    expect(p?.binKey).toBe('cell:-5:-60')
    expect(p?.lat).toBe(-4.5)
    expect(p?.lon).toBe(-59.5)
    expect(p?.iso2).toBeNull()
  })

  it('places a country bin at its gazetteer centroid', () => {
    const p = placeBinLabel('IQ')
    expect(p?.binKind).toBe('country')
    expect(p?.binKey).toBe('country:IQ')
    expect(p?.iso2).toBe('IQ')
    expect(Number.isFinite(p?.lat)).toBe(true)
  })

  it('returns null for an unrecognized label (never a fabricated point)', () => {
    expect(placeBinLabel('somewhere')).toBeNull()
    expect(placeBinLabel('ZZ')).toBeNull() // not a real country
  })
})

describe('convergence — parseConvergenceAlert', () => {
  it('parses a formation title into a placed marker + counts', () => {
    const m = parseConvergenceAlert(
      row({
        id: 'f1',
        summary: 'Geo convergence formed: cell(33..34°, 44..45°) IQ — 3 source families, 12 signals (24h)',
      }),
    )
    expect(m).toMatchObject({
      id: 'f1',
      event: 'formed',
      binKind: 'cell',
      binKey: 'cell:33:44',
      iso2: 'IQ',
      familyCount: 3,
      signalCount: 12,
      windowHours: 24,
      lat: 33.5,
      lon: 44.5,
    })
  })

  it('parses a country formation title', () => {
    const m = parseConvergenceAlert(
      row({ summary: 'Geo convergence formed: IQ — 4 source families, 30 signals (24h)' }),
    )
    expect(m?.binKind).toBe('country')
    expect(m?.familyCount).toBe(4)
  })

  it('parses a dissolution title (counts null)', () => {
    const m = parseConvergenceAlert(
      row({
        summary: 'Geo convergence dissolved: cell(33..34°, 44..45°) IQ — below 3 distinct source families (24h window)',
      }),
    )
    expect(m?.event).toBe('dissolved')
    expect(m?.familyCount).toBeNull()
    expect(m?.binKey).toBe('cell:33:44')
  })

  it('returns null for a non-convergence / unparseable title', () => {
    expect(parseConvergenceAlert(row({ summary: 'Band crossing: US moved high→low' }))).toBeNull()
    expect(parseConvergenceAlert(row({ summary: '' }))).toBeNull()
  })
})

describe('convergence — activeConvergenceMarkers', () => {
  it('keeps geo_convergence rows only, latest event per bin wins', () => {
    const rows: SinceAlertRow[] = [
      row({
        id: 'other',
        channel: 'band_crossing',
        summary: 'Geo convergence formed: IQ — 3 source families, 9 signals (24h)',
      }),
      row({
        id: 'iq-old',
        produced_at: '2026-07-25T00:00:00Z',
        summary: 'Geo convergence formed: cell(33..34°, 44..45°) IQ — 3 source families, 9 signals (24h)',
      }),
      row({
        id: 'iq-new',
        produced_at: '2026-07-26T00:00:00Z',
        summary: 'Geo convergence formed: cell(33..34°, 44..45°) IQ — 5 source families, 40 signals (24h)',
      }),
    ]
    const markers = activeConvergenceMarkers(rows)
    // band_crossing row excluded even though its title looks convergent
    expect(markers).toHaveLength(1)
    expect(markers[0].id).toBe('iq-new')
    expect(markers[0].familyCount).toBe(5)
  })

  it('drops a bin whose latest event is a dissolution', () => {
    const rows: SinceAlertRow[] = [
      row({
        id: 'formed',
        produced_at: '2026-07-25T00:00:00Z',
        summary: 'Geo convergence formed: cell(1..2°, 3..4°) — 3 source families, 10 signals (24h)',
      }),
      row({
        id: 'dissolved',
        severity: 'info',
        produced_at: '2026-07-26T00:00:00Z',
        summary: 'Geo convergence dissolved: cell(1..2°, 3..4°) — below 3 distinct source families (24h window)',
      }),
    ]
    expect(activeConvergenceMarkers(rows)).toEqual([])
  })
})
