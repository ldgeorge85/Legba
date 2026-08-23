/**
 * Tests for the narratives pure model.
 *
 * The server attaches an `honesty_note` to every envelope so a client cannot
 * render echo-lead as a causal claim. These tests pin the two ways this layer
 * holds that line: the note always reaches the surface (even on a response that
 * predates the field), and the labels grade CO-CARRIAGE CONSISTENCY without ever
 * upgrading an edge to "systematic" on their own.
 */

import { describe, it, expect } from 'vitest'
import type { Narrative, NarrativeEchoEdge } from '@/lib/api'
import {
  HONESTY_NOTE_FALLBACK,
  carrierViews,
  datedCoverage,
  echoStrengthLabel,
  echoTone,
  formatLagHours,
  honestyNote,
  narrativeStatusTone,
  narrativeTitle,
  variantViews,
} from './narrativesModel'

function narrative(over: Partial<Narrative> = {}): Narrative {
  return {
    contention_id: 'c1',
    subject_key: 'Strait of Hormuz',
    predicate_key: 'transit_status',
    status: 'contested',
    surfaced_value: null,
    variant_count: 2,
    carrier_source_count: 4,
    publish_dated_source_count: 2,
    signal_count: 9,
    fact_count: 1,
    first_seen_at: '2026-08-01T00:00:00Z',
    last_seen_at: '2026-08-03T00:00:00Z',
    span_hours: 48,
    lead_source_id: 'src.a',
    lead_first_seen_at: '2026-08-01T00:00:00Z',
    max_echo_lag_hours: 6,
    carriers: [],
    variants: [],
    opened_at: null,
    contention_surfaced_at: null,
    computed_at: null,
    ...over,
  }
}

function edge(over: Partial<NarrativeEchoEdge> = {}): NarrativeEchoEdge {
  return {
    leader_source_id: 'src.a',
    follower_source_id: 'src.b',
    co_carried: 10,
    lead_count: 9,
    follow_within_count: 8,
    echo_ratio: 0.9,
    median_lag_hours: 3,
    mean_lag_hours: 3.4,
    min_lag_hours: 1,
    max_lag_hours: 9,
    echo_window_hours: 24,
    systematic: false,
    computed_at: null,
    ...over,
  }
}

describe('honestyNote', () => {
  it('renders the server note verbatim when present', () => {
    expect(honestyNote({ honesty_note: 'server says so' })).toBe('server says so')
  })

  it('falls back to the verbatim contract rather than dropping it', () => {
    expect(honestyNote(undefined)).toBe(HONESTY_NOTE_FALLBACK)
    expect(honestyNote({})).toBe(HONESTY_NOTE_FALLBACK)
  })

  it('the fallback still refuses the causal reading', () => {
    expect(HONESTY_NOTE_FALLBACK).toContain('NOT a causal or coordination claim')
  })
})

describe('formatLagHours', () => {
  it('keeps an unmeasured lag as "—" rather than rendering it as zero', () => {
    expect(formatLagHours(null)).toBe('—')
    expect(formatLagHours(undefined)).toBe('—')
    expect(formatLagHours(Number.NaN)).toBe('—')
  })

  it('scales minutes / hours / days', () => {
    expect(formatLagHours(0.5)).toBe('30m')
    expect(formatLagHours(3.25)).toBe('3.3h')
    expect(formatLagHours(72)).toBe('3.0d')
  })
})

describe('datedCoverage — the denominator the echo timing rests on', () => {
  it('reports dated carriers against the total', () => {
    expect(datedCoverage(narrative())).toEqual({ dated: 2, total: 4, ratio: 0.5 })
  })

  it('returns null when there are no carriers (no ratio to state)', () => {
    expect(datedCoverage(narrative({ carrier_source_count: 0 }))).toBeNull()
  })
})

describe('echoStrengthLabel — grades the observation, not influence', () => {
  it('bands by co-carriage ratio', () => {
    expect(echoStrengthLabel(edge({ echo_ratio: 0.95 }))).toBe('consistent')
    expect(echoStrengthLabel(edge({ echo_ratio: 0.6 }))).toBe('frequent')
    expect(echoStrengthLabel(edge({ echo_ratio: 0.2 }))).toBe('occasional')
  })

  it('says unrated rather than guessing when the ratio is absent', () => {
    expect(echoStrengthLabel(edge({ echo_ratio: null }))).toBe('unrated')
  })

  it('never promotes an edge to systematic on its own — that is the server flag', () => {
    const strong = edge({ echo_ratio: 0.99, systematic: false })
    expect(echoStrengthLabel(strong)).toBe('consistent')
    expect(echoTone(strong)).not.toContain('amber')
    expect(echoTone(edge({ echo_ratio: 0.1, systematic: true }))).toContain('amber')
  })
})

describe('narrativeTitle / narrativeStatusTone', () => {
  it('reads a family as subject · predicate', () => {
    expect(narrativeTitle(narrative())).toBe('Strait of Hormuz · transit_status')
  })

  it('tones contested and surfaced differently, and anything else neutrally', () => {
    expect(narrativeStatusTone('contested')).toContain('rose')
    expect(narrativeStatusTone('surfaced')).toContain('emerald')
    expect(narrativeStatusTone('weird')).toContain('border-line')
  })
})

describe('carrierViews / variantViews read free-form jsonb defensively', () => {
  it('pulls the known fields and leaves the rest absent rather than guessed', () => {
    const views = carrierViews(
      narrative({
        carriers: [
          { source_id: 'src.a', first_seen_at: '2026-08-01T00:00:00Z', lag_hours: 0, signal_count: 3 },
          { sourceId: 'src.b' },
          {},
        ],
      }),
    )
    expect(views[0]).toEqual({
      sourceId: 'src.a',
      firstSeenAt: '2026-08-01T00:00:00Z',
      lagHours: 0,
      signalCount: 3,
    })
    expect(views[1]).toEqual({
      sourceId: 'src.b',
      firstSeenAt: null,
      lagHours: null,
      signalCount: null,
    })
    expect(views[2].sourceId).toBe('(unknown source)')
  })

  it('labels an unlabeled variant instead of rendering undefined', () => {
    const views = variantViews(
      narrative({ variants: [{ value: 'closed', count: 4 }, { signal_count: 2 }, {}] }),
    )
    expect(views[0]).toEqual({ value: 'closed', count: 4 })
    expect(views[1]).toEqual({ value: '(unlabeled variant)', count: 2 })
    expect(views[2]).toEqual({ value: '(unlabeled variant)', count: null })
  })
})
