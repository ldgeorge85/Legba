/**
 * Unit tests for the typed-absence reader model (7b/k5).
 *
 * The three things the drill depends on and a blank would quietly get wrong:
 * the selection id round-trips exactly (and refuses anything malformed rather
 * than half-matching); "not checked" stays distinguishable from "nothing
 * absent"; and a stale item states itself as LAST KNOWN, NOT RE-CHECKED
 * instead of reading as a current absence.
 */
import { describe, it, expect } from 'vitest'
import {
  ABSENCE_KIND_LABEL,
  absenceExtent,
  absenceHeadline,
  absenceId,
  matchAbsence,
  matchAbsenceSubject,
  notMeasuredReason,
  parseAbsenceId,
  type AbsenceItem,
  type AbsenceResponse,
} from './absenceModel'

const NOW = '2026-09-24T12:00:00.000Z'

function item(over: Partial<AbsenceItem> = {}): AbsenceItem {
  return {
    kind: 'source_stale',
    subject: 'energy_security',
    since: '2026-09-22T20:00:00.000Z',
    window: null,
    reason: 'the latest read is 40.0h old',
    as_of: '2026-09-22T20:00:00.000Z',
    as_of_basis: "this unit's last run for this desk",
    expires_at: '2026-09-23T01:00:00.000Z',
    review: null,
    stale: true,
    proof: {
      what_was_checked: "the latest kind='finding' row for analyst 'energy_security'",
      checked_at: '2026-09-22T20:00:00.000Z',
      ref: 'f-es',
      ref_kind: 'finding',
    },
    ...over,
  }
}

function resp(over: Partial<AbsenceResponse> = {}): AbsenceResponse {
  return {
    version: '2026-09/k5',
    scope: 'israel',
    read_at: NOW,
    kinds: { ...ABSENCE_KIND_LABEL },
    absences: [item()],
    not_measured: [],
    ...over,
  }
}

describe('the selection id', () => {
  it('round-trips a scope, kind and subject exactly', () => {
    const id = absenceId('country_watch_il', 'below_floor', 'escalation')
    expect(id).toBe('country_watch_il|below_floor|escalation')
    expect(parseAbsenceId(id)).toEqual({
      scope: 'country_watch_il',
      kind: 'below_floor',
      subject: 'escalation',
    })
  })

  it('round-trips a dotted source id and a hex claim key', () => {
    for (const subject of ['source.jpost.frontpage', 'a3f9c1d0e4b7']) {
      const parsed = parseAbsenceId(absenceId('israel', 'collected_but_silent', subject))
      expect(parsed?.subject).toBe(subject)
    }
  })

  it('round-trips the eighth kind, whose subject carries a colon', () => {
    // 7g-2 — `history_gap`'s subject is `<series_id>:<subject>`, which is a
    // shape no earlier kind produced. The id splits on `|`, so the colon is
    // just a character; this pins that it stays one.
    const id = absenceId('country_g20_us', 'history_gap', 'wb.gdp_growth_annual_pct:US')
    expect(parseAbsenceId(id)).toEqual({
      scope: 'country_g20_us',
      kind: 'history_gap',
      subject: 'wb.gdp_growth_annual_pct:US',
    })
  })

  it('refuses a malformed or unknown-kind id rather than half-matching', () => {
    expect(parseAbsenceId('israel|below_floor')).toBeNull()
    expect(parseAbsenceId('israel||escalation')).toBeNull()
    expect(parseAbsenceId('israel|made_up_kind|escalation')).toBeNull()
    expect(parseAbsenceId('a|b|c|d')).toBeNull()
    expect(parseAbsenceId('')).toBeNull()
  })
})

describe('matching', () => {
  it('finds the item for a kind and subject', () => {
    expect(matchAbsence(resp(), 'source_stale', 'energy_security')?.subject).toBe(
      'energy_security',
    )
    expect(matchAbsence(resp(), 'below_floor', 'energy_security')).toBeNull()
    expect(matchAbsence(null, 'source_stale', 'energy_security')).toBeNull()
  })

  it('matches a unit across the unit kinds, which the route owns', () => {
    // The cell knows the unit is silent; whether that is `not_collected` or
    // `source_stale` is the route's call, so a subject match must span both.
    const r = resp({ absences: [item({ kind: 'not_collected' })] })
    expect(
      matchAbsenceSubject(r, 'energy_security', ['not_collected', 'source_stale'])?.kind,
    ).toBe('not_collected')
    expect(matchAbsenceSubject(r, 'energy_security', ['below_floor'])).toBeNull()
  })
})

describe('not_measured', () => {
  it('keeps "not checked" distinguishable from "nothing absent"', () => {
    const r = resp({
      absences: [],
      not_measured: [
        'below_floor: no banded scorecard has been computed for this desk yet',
      ],
    })
    expect(notMeasuredReason(r, 'below_floor')).toBe(
      'no banded scorecard has been computed for this desk yet',
    )
    // Read cleanly and found nothing — NOT the same answer, and not reported
    // as one.
    expect(notMeasuredReason(r, 'source_stale')).toBeNull()
  })

  it('matches a compound entry naming several kinds', () => {
    const r = resp({
      not_measured: [
        'collected_but_silent / source_stale (sources): this desk declares no scope.geo',
      ],
    })
    expect(notMeasuredReason(r, 'collected_but_silent')).toBe(
      'this desk declares no scope.geo',
    )
  })
})

describe('extent and headline', () => {
  it('prefers a recorded instant, falls back to the stated window', () => {
    expect(absenceExtent(item())).toContain('since')
    expect(absenceExtent(item({ since: null, window: 'no read on record' }))).toBe(
      'no read on record',
    )
    expect(absenceExtent(item({ since: null, window: null }))).toBe(
      'extent not recorded',
    )
  })

  it('states a stale absence as last known, not re-checked', () => {
    const line = absenceHeadline(item({ stale: true }))
    expect(line).toContain('last known absence, not re-checked')
    expect(line).toContain("this unit's last run for this desk")
  })

  it('states a current absence as current until its next check', () => {
    const line = absenceHeadline(item({ stale: false }))
    expect(line).toContain('current until')
    expect(line).not.toContain('not re-checked')
  })

  it('names the review path where no clock governs it', () => {
    const line = absenceHeadline(
      item({ kind: 'layer_declared_absent', expires_at: null, review: 'map revision' }),
    )
    expect(line).toContain('revised by map revision')
  })

  it('never invents a measurement instant', () => {
    const line = absenceHeadline(
      item({ as_of: null, as_of_basis: 'no run and no scan on record', expires_at: null }),
    )
    expect(line).toContain('not measured — no run and no scan on record')
  })
})
