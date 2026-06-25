/**
 * Unit tests for the UI-6 alert / notification-center model (DOM-free).
 */

import { describe, it, expect, beforeEach } from 'vitest'
import {
  annotateMatch,
  loadSubscriptions,
  mapAlertEnvelope,
  matchSubscription,
  persistSubscriptions,
  removeSubscription,
  severityAtLeast,
  subId,
  toggleMute,
  upsertSubscription,
  type AlertSubscription,
  type FiredAlert,
} from './alertModel'

function sub(over: Partial<AlertSubscription> = {}): AlertSubscription {
  return {
    id: subId('target', 'brazil'),
    scope_kind: 'target',
    scope_id: 'brazil',
    severity_floor: 'high',
    muted: false,
    created_at: '2026-06-03T00:00:00Z',
    ...over,
  }
}

beforeEach(() => localStorage.clear())

describe('persistence', () => {
  it('round-trips through localStorage', () => {
    persistSubscriptions([sub()])
    expect(loadSubscriptions()).toHaveLength(1)
  })
  it('returns [] on missing / malformed storage', () => {
    expect(loadSubscriptions()).toEqual([])
    localStorage.setItem('legba.alerts.subscriptions', '{bad json')
    expect(loadSubscriptions()).toEqual([])
  })
})

describe('mutation helpers', () => {
  it('upsert replaces by id', () => {
    const a = upsertSubscription([], sub())
    const b = upsertSubscription(a, sub({ severity_floor: 'critical' }))
    expect(b).toHaveLength(1)
    expect(b[0].severity_floor).toBe('critical')
  })
  it('remove + toggleMute', () => {
    const a = upsertSubscription([], sub())
    expect(removeSubscription(a, subId('target', 'brazil'))).toHaveLength(0)
    expect(toggleMute(a, subId('target', 'brazil'))[0].muted).toBe(true)
  })
})

describe('severityAtLeast', () => {
  it('respects ordering and nulls', () => {
    expect(severityAtLeast('critical', 'high')).toBe(true)
    expect(severityAtLeast('medium', 'high')).toBe(false)
    expect(severityAtLeast(null, 'low')).toBe(false)
  })
})

describe('mapAlertEnvelope', () => {
  it('maps a well-formed envelope', () => {
    const a = mapAlertEnvelope({
      id: 'al1',
      title: 'Coup alert',
      severity: 'critical',
      target_id: 'brazil',
      produced_at: '2026-06-03T01:00:00Z',
    })
    expect(a).toMatchObject({ id: 'al1', severity: 'critical', target_id: 'brazil', matched_sub_id: null })
  })
  it('rejects id-less envelopes and unknown severities', () => {
    expect(mapAlertEnvelope({ title: 'x' })).toBeNull()
    expect(mapAlertEnvelope(undefined)).toBeNull()
    expect(mapAlertEnvelope({ id: 'a', severity: 'bogus' })!.severity).toBeNull()
  })
})

describe('matchSubscription / annotateMatch', () => {
  const fired = (over: Partial<FiredAlert> = {}): FiredAlert => ({
    id: 'al1',
    title: 't',
    severity: 'critical',
    target_id: 'brazil',
    analyst_id: null,
    fired_at: '2026-06-03T00:00:00Z',
    matched_sub_id: null,
    ...over,
  })

  it('matches scope + meets floor', () => {
    expect(matchSubscription(fired(), [sub()])).toBe(subId('target', 'brazil'))
  })
  it('no match below floor', () => {
    expect(matchSubscription(fired({ severity: 'low' }), [sub()])).toBeNull()
  })
  it('no match on muted subs', () => {
    expect(matchSubscription(fired(), [sub({ muted: true })])).toBeNull()
  })
  it('no match on different scope', () => {
    expect(matchSubscription(fired({ target_id: 'iran' }), [sub()])).toBeNull()
  })
  it('analyst-scoped match uses analyst_id', () => {
    const s = sub({ id: subId('analyst', 'cred'), scope_kind: 'analyst', scope_id: 'cred', severity_floor: 'low' })
    expect(matchSubscription(fired({ target_id: null, analyst_id: 'cred' }), [s])).toBe(subId('analyst', 'cred'))
  })
  it('annotateMatch stamps the matched id', () => {
    expect(annotateMatch(fired(), [sub()]).matched_sub_id).toBe(subId('target', 'brazil'))
  })
})
