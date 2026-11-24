/**
 * The Watch model (U-9): plain words, the page decision, the two sides of a
 * silence, the edit record as a measure. Invented example rows only.
 */
import { describe, expect, it } from 'vitest'
import { WATCH_ALERT_GROUPS, WATCH_SEVERITY_FLOORS, declaredContract } from '@/panel-registry/contracts'
import { PROFILE_UI } from '@/lib/profileUi'
import {
  ALERT_GROUP_LABEL,
  DEFAULT_ALERT_FILTER,
  QUIET_CLASSES,
  SEVERITIES,
  agoText,
  alertRows,
  changeWords,
  editRecordRows,
  filterAlerts,
  firingCensus,
  flagRows,
  kindWords,
  pageDecision,
  parseRuleEntry,
  quietClassOf,
  sourceLabel,
  sourceSilences,
  subjectOf,
  unitSilences,
  whenText,
  windowHours,
} from './model'
import type { AlertEpisode, AlertLogResponse, FiringRow } from './types'

const NOW = Date.parse('2026-11-14T12:00:00Z')

function ep(over: Partial<AlertEpisode>): AlertEpisode {
  return {
    id: 'a1',
    kind: 'source_cadence_stall',
    group: 'source_health',
    subject_kind: 'source',
    subject_id: 'source.example.harbour_notices',
    severity: 'high',
    max_severity: 'high',
    summary: 'Example summary',
    opened_at: '2026-11-14T10:00:00Z',
    last_raised_at: '2026-11-14T11:00:00Z',
    occurrences: 1,
    paged_count: 0,
    ...over,
  }
}

const LOG: AlertLogResponse = {
  measured: true,
  window_hours: 24,
  rule_version: '2026-11-14/1',
  kinds: [
    { kind: 'source_cadence_stall', group: 'source_health', pages_when: ['source_cadence_stall >= high'] },
    { kind: 'baseline_deviation', group: 'baseline', pages_when: [] },
    { kind: 'volume_surge', group: 'baseline', pages_when: ['volume_surge >= medium'] },
    { kind: 'host_disk', group: 'host', pages_when: ['host_disk >= low'] },
    { kind: 'analyst_hard_fail', group: 'analyst_failure', pages_when: ['analyst_hard_fail >= high'] },
  ],
  open: [
    ep({ id: 'a1', paged_count: 1, paged_severity: 'high' }),
    ep({ id: 'a2', kind: 'baseline_deviation', group: 'baseline', subject_kind: 'target', subject_id: 'example_land', severity: 'medium', max_severity: 'medium', last_raised_at: '2026-11-14T11:30:00Z' }),
    ep({ id: 'a3', kind: 'host_disk', group: 'host', subject_kind: 'host', subject_id: 'host', severity: 'low', max_severity: 'low', paged_count: 1, paged_severity: 'low', last_raised_at: '2026-11-14T09:00:00Z' }),
    ep({ id: 'a4', kind: 'analyst_hard_fail', group: 'analyst_failure', subject_kind: 'analyst', subject_id: 'example_desk', severity: 'medium', max_severity: 'medium', last_raised_at: '2026-11-14T08:00:00Z' }),
  ],
  closed: [
    ep({ id: 'c1', kind: 'volume_surge', group: 'baseline', subject_kind: 'target', subject_id: 'example_land', severity: 'medium', max_severity: 'medium', paged_count: 1, paged_severity: 'medium', closed_at: '2026-11-14T11:45:00Z', close_reason: 'quiet: no raise for 6 h', last_raised_at: '2026-11-14T05:00:00Z' }),
  ],
  paged: [{ event_id: 'e1', alert_id: 'a1', raised_at: '2026-11-14T10:00:00Z', kind: 'source_cadence_stall', severity: 'high', rule_entry: 'source_cadence_stall >= high', outcomes: ['delivered'], delivered: true }],
}

describe('time and window', () => {
  it('prints UTC times and ages, and nothing for an unreadable time', () => {
    expect(whenText('2026-11-14T09:05:00Z')).toBe('14 Nov 09:05Z')
    expect(whenText('nope')).toBe('')
    expect(agoText('2026-11-14T09:00:00Z', NOW)).toBe('3 h ago')
    expect(agoText('2026-11-12T12:00:00Z', NOW)).toBe('2 d ago')
    expect(agoText(null, NOW)).toBe('')
  })
  it('the log window follows the one clock, clamped to what the route takes', () => {
    expect(windowHours(24 * 3_600_000)).toBe(24)
    expect(windowHours(30 * 24 * 3_600_000)).toBe(720)
    expect(windowHours(60 * 24 * 3_600_000)).toBe(720)
    expect(windowHours(0)).toBe(1)
  })
})

describe('subjects', () => {
  it('names a source, a unit and a subject; the system is not a point', () => {
    expect(sourceLabel('source.example.harbour_notices')).toBe('example · harbour notices')
    expect(subjectOf('source', 'source.x.y').kind).toBe('source')
    expect(subjectOf('analyst', 'example_desk').kind).toBe('analyst')
    expect(subjectOf('target', 'example_land').kind).toBe('target')
    expect(subjectOf('host', 'host')).toMatchObject({ kind: null, label: 'the host' })
  })
})

describe('the alert log', () => {
  const rows = alertRows(LOG)
  it('lists open and closed episodes newest first', () => {
    expect(rows.map((r) => r.id)).toEqual(['c1', 'a2', 'a1', 'a3', 'a4'])
    expect(rows.find((r) => r.id === 'c1')!.stateWords).toBe('closed 14 Nov 11:45Z — quiet for 6 h')
    expect(rows.find((r) => r.id === 'a1')!.stateWords).toBe('open since 14 Nov 10:00Z')
  })
  it('says a kind in plain words, never its id', () => {
    for (const r of rows) {
      expect(r.title).not.toMatch(/_/)
      expect(r.groupLabel).not.toMatch(/_/)
    }
    expect(kindWords('source_cadence_stall')).toBe('stopped arriving on schedule')
    expect(kindWords('a_new_kind')).toBe('a new kind')
  })
  it('groups kinds for the reader; the system kinds are their own group', () => {
    const g = Object.fromEntries(rows.map((r) => [r.id, r.group]))
    expect(g).toEqual({ c1: 'fired', a2: 'fired', a1: 'quiet', a3: 'system', a4: 'desk' })
  })
  it('says whether it paged and by which entry of the rule, or why it was only logged', () => {
    const by = Object.fromEntries(rows.map((r) => [r.id, r.decision]))
    expect(by.a1).toEqual({ paged: true, words: 'Paged at high', rule: 'the page rule pages this kind at high or above', delivered: true })
    expect(by.a2).toMatchObject({ paged: false, words: 'Logged only', rule: 'the page rule has no entry for this kind' })
    expect(by.a4.rule).toBe('the page rule pages this kind at high or above; this reached medium')
    // paged, but its page fell outside the window: the kind's rule names it, delivery unknown
    expect(by.c1).toMatchObject({ paged: true, rule: 'the page rule pages this kind at medium or above', delivered: null })
  })
  it('a rule that was not read is said, not guessed', () => {
    expect(pageDecision(ep({}), new Map(), []).rule).toBe('the page rule was not read')
  })
  it('parses a rule entry with and without a subject', () => {
    expect(parseRuleEntry('volume_surge >= medium')).toEqual({ min: 'medium', subject: null })
    expect(parseRuleEntry('volume_surge >= medium (subject US)')).toEqual({ min: 'medium', subject: 'US' })
    expect(parseRuleEntry('garbage')).toBeNull()
  })
  it('filters by group, severity floor, paged and state — over what was read', () => {
    expect(filterAlerts(rows, DEFAULT_ALERT_FILTER).map((r) => r.id)).toEqual(['c1', 'a2', 'a1', 'a4'])
    expect(filterAlerts(rows, { ...DEFAULT_ALERT_FILTER, group: 'all' })).toHaveLength(5)
    expect(filterAlerts(rows, { ...DEFAULT_ALERT_FILTER, severity: 'high' }).map((r) => r.id)).toEqual(['a1'])
    expect(filterAlerts(rows, { ...DEFAULT_ALERT_FILTER, paged: 'paged' }).map((r) => r.id)).toEqual(['c1', 'a1'])
    expect(filterAlerts(rows, { ...DEFAULT_ALERT_FILTER, paged: 'logged', state: 'open' }).map((r) => r.id)).toEqual(['a2', 'a4'])
    expect(filterAlerts(rows, { ...DEFAULT_ALERT_FILTER, group: 'quiet' }).map((r) => r.id)).toEqual(['a1'])
  })
  it('an unmeasured log has no rows (never a fake empty list)', () => {
    expect(alertRows({ measured: false, window_hours: 24 })).toEqual([])
    expect(alertRows(null)).toEqual([])
  })
  it("the contract's enums are the model's options", () => {
    expect([...WATCH_ALERT_GROUPS].sort()).toEqual(['all', 'reader', ...Object.keys(ALERT_GROUP_LABEL)].sort())
    // roll 84: the chip names the reader's noun, not the engine's unit
    expect(ALERT_GROUP_LABEL.desk).toBe(`${PROFILE_UI.subject.singular} stalled`)
    expect(ALERT_GROUP_LABEL.desk).not.toMatch(/\bunit\b/i)
    expect([...WATCH_SEVERITY_FLOORS]).toEqual(['any', ...SEVERITIES])
    for (const id of ['view:watch_alerts', 'view:watch_silences', 'view:watch_edits']) {
      expect(declaredContract(id)?.side).toBe('reader')
    }
  })
})

describe('silences', () => {
  const firing: FiringRow[] = [
    { source_id: 'source.example.a', state: 'active', status: 'firing', last_seen_at: '2026-11-14T11:00:00Z' },
    { source_id: 'source.example.b', state: 'active', status: 'silent', last_seen_at: '2026-11-11T11:00:00Z', quiet_class: 'published_nothing' },
    { source_id: 'source.example.c', state: 'active', status: 'error', last_seen_at: '2026-11-10T11:00:00Z', quiet_class: 'polling_fails' },
    { source_id: 'source.example.d', state: 'active', status: 'silent', last_seen_at: null, quiet_class: 'never_produced' },
    { source_id: 'source.example.e', state: 'paused', status: 'paused' },
  ]
  it('puts each quiet source on its side, could-not-look first', () => {
    const s = sourceSilences(firing, NOW)
    expect(s.map((r) => [r.ref.id, r.def.side])).toEqual([
      ['source.example.c', 'could_not_look'],
      ['source.example.d', 'unknown'],
      ['source.example.b', 'looked'],
    ])
    expect(s[2].since).toBe('last item 3 d ago')
    expect(s[1].since).toBe('nothing ever read')
  })
  it('counts active, quiet and paused from the same rows', () => {
    expect(firingCensus(firing)).toEqual({ active: 4, quiet: 3, paused: 1 })
  })
  it('without the server class it never guesses the published / not-taken-in split', () => {
    expect(quietClassOf({ source_id: 'x', state: 'active', status: 'silent', last_seen_at: '2026-11-10T00:00:00Z', last_poll_outcome: 'empty' })).toBe('unrecorded')
    expect(quietClassOf({ source_id: 'x', state: 'active', status: 'silent', last_seen_at: '2026-11-10T00:00:00Z', last_poll_outcome: 'gated' })).toBe('filtered_to_nothing')
    expect(quietClassOf({ source_id: 'x', state: 'active', status: 'error', last_poll_outcome: 'error' })).toBe('polling_fails')
    expect(quietClassOf({ source_id: 'x', state: 'active', status: 'silent', last_seen_at: null })).toBe('never_produced')
    // an unknown class from a newer server reads as unrecorded, not as nothing
    expect(quietClassOf({ source_id: 'x', state: 'active', status: 'silent', quiet_class: 'a_new_reason' })).toBe('unrecorded')
  })
  it('every class has words and a side', () => {
    for (const def of Object.values(QUIET_CLASSES)) {
      expect(def.title).not.toMatch(/_/)
      expect(['looked', 'could_not_look', 'unknown']).toContain(def.side)
    }
  })
  it("units come from the log's open unit episodes, once each", () => {
    const u = unitSilences(LOG, NOW)
    expect(u.map((r) => [r.ref.id, r.def.side, r.def.title])).toEqual([['example_desk', 'could_not_look', 'Its runs fail']])
  })
})

describe('source edits', () => {
  it('says the change, the acknowledgement and what relied on it in words; keeps both passages and the shadow', () => {
    const [f] = flagRows([
      {
        id: 7,
        address: 'https://example.org/news/item-1',
        source_id: 'source.example.harbour_notices',
        drift_class: 'substantive_edit',
        change_type: 'fact_corrected',
        acknowledged: 'silent',
        claim_effect: 'cited_text_changed',
        dependant_kind: 'finding',
        dependant_id: 'f-1',
        earlier_fetched_at: '2026-11-12T09:10:00Z',
        later_fetched_at: '2026-11-13T07:02:00Z',
        changed: [{ earlier: 'Twelve ships waited.', later: 'Nine ships waited.' }],
        removed: ['A removed sentence.'],
        would_auto_close: true,
      },
    ])
    expect(f).toMatchObject({
      id: '7',
      page: 'example.org/news/item-1',
      change: 'a fact corrected',
      ack: 'without saying so',
      effect: 'the passage relied on changed',
      when: 'read 12 Nov 09:10Z · changed by 13 Nov 07:02Z',
      wouldAutoClose: true,
    })
    expect(f.reliedOn).toMatchObject({ kind: 'finding', id: 'f-1' })
    expect(f.passages).toEqual([{ earlier: 'Twelve ships waited.', later: 'Nine ships waited.' }])
    expect(changeWords({ change_type: null, drift_class: 'gone' })).toBe('the page is gone')
  })
  it('the edit record is listed by name, never by how much a source edits', () => {
    const rows = editRecordRows([
      { subject: 'source.zeta.wire', substantive_or_worse: 9, edit_record: { '30d': { pages_rechecked: 40, assessed: 9, acknowledged: { silent: 5 } } } },
      { subject: 'source.alpha.daily', substantive_or_worse: 1, edit_record: { '30d': { pages_rechecked: 12, assessed: 1, history_rewrite: 0 } } },
    ])
    expect(rows.map((r) => r.id)).toEqual(['source.alpha.daily', 'source.zeta.wire'])
    expect(rows[1]).toMatchObject({ rechecked: 40, assessed: 9, silent: 5, notice: null })
    expect(rows[0].rewrites).toBe(0)
  })
})
