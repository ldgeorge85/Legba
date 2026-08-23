/**
 * Component test for the `system.source_health` panel.
 *
 * Mocks at the HTTP boundary and routes by URL — three endpoints are in play:
 *   GET /v3/system/staleness-debt      → the debt strip
 *   GET /v3/source-quality             → the rollup table (a BARE ARRAY)
 *   GET /v3/sources/{id}/quality       → the lazy per-source drill-down
 *
 * What these tests exist to hold — every one of them is a way the panel could
 * quietly flatter the data:
 *   * asserted and earned are separately LABELLED and never merged;
 *   * a 100% win rate over n=2 is flagged and carries its n;
 *   * `earned: null` (no row) renders differently from `contested_total: 0`
 *     (a row that was never contested) — neither reads as a zero;
 *   * a 503 is "the view isn't provisioned", not "your sources are broken";
 *   * the staleness strip renders `by_reason` AND the match_verified caveat;
 *   * the drill-down is lazy — nothing is fetched for an unopened row.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import SourceHealthPanel from './SourceHealth'
import { mockErrorResponse } from '@/test/apiMocks'
import type {
  AssertedQuality,
  ComputedQuality,
  SourceEarned,
  SourceQualityDetail,
  SourceQualityRow,
  StalenessDebtResponse,
} from '@/lib/api'
import type { PanelRegistration } from '@/types'

function reg(): PanelRegistration {
  return {
    id: 'sh1',
    panel_id: 'system_source_health',
    descriptor_id: '(singleton)',
    descriptor_version: '0'.repeat(64),
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Source Health',
    mode: 'personal',
    layout_slot: 'main',
    data_query: {},
    binding: {},
    retired: false,
    created_at: '2026-08-01T00:00:00Z',
    retired_at: null,
  }
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function asserted(over: Partial<AssertedQuality> = {}): AssertedQuality {
  return {
    admiralty_reliability: null,
    admiralty_credibility: null,
    admiralty_grade: null,
    admiralty_rater: null,
    admiralty_method: null,
    admiralty_rated_at: null,
    public_rating_count: 0,
    private_rating_count: 0,
    has_dossier: false,
    dossier_compiled_at: null,
    dossier_compiled_by: null,
    host_matched: null,
    host_score: null,
    host_tier: null,
    host_state_affiliation: null,
    host_rationale: null,
    host_scored_by: null,
    host_scored_at: null,
    ...over,
  }
}

function computed(over: Partial<ComputedQuality> = {}): ComputedQuality {
  return {
    freshness_grade: 'ok',
    budget_minutes: 60,
    cadence_raw: '1h',
    last_signal_at: '2026-08-20T11:00:00Z',
    age_seconds: 600,
    signals_24h: 12,
    signals_7d: 80,
    ...over,
  }
}

function earned(over: Partial<SourceEarned> = {}): SourceEarned {
  return {
    wins: 140,
    losses: 60,
    contested_total: 200,
    win_rate_raw: 0.7,
    win_rate_smoothed: 0.7,
    win_rate_lower: 0.64,
    low_sample: false,
    corroborated: 40,
    corroboration_total: 100,
    corroboration_rate: 0.4,
    lag_hours: 2,
    sample_as_of: '2026-08-19T00:00:00Z',
    computed_at: '2026-08-20T00:00:00Z',
    ...over,
  }
}

/** The flagship case: asserts A1, has been measured over 200 contests. */
const MEASURED: SourceQualityRow = {
  source_id: 'src-measured',
  registered: true,
  declared_state: 'active',
  declared_kind: 'rss',
  endpoint_host: 'measured.example',
  asserted: asserted({
    admiralty_grade: 'B2',
    admiralty_rater: 'ops-desk',
    public_rating_count: 3,
  }),
  earned: earned(),
  computed: computed(),
}

/** 100% over TWO contests — must never render like 100% over 200. */
const LOW_SAMPLE: SourceQualityRow = {
  source_id: 'src-lowsample',
  registered: true,
  declared_state: 'active',
  declared_kind: 'api',
  endpoint_host: 'thin.example',
  asserted: asserted({ admiralty_grade: 'A1', admiralty_rater: 'self' }),
  earned: earned({
    wins: 2,
    losses: 0,
    contested_total: 2,
    win_rate_raw: 1,
    win_rate_smoothed: 0.6,
    win_rate_lower: 0.21,
    low_sample: true,
    corroborated: 1,
    corroboration_total: 2,
    corroboration_rate: null,
  }),
  computed: computed({ freshness_grade: 'ungraded', budget_minutes: null }),
}

/** NO track-record row at all — nothing was ever measured. */
const NO_RECORD: SourceQualityRow = {
  source_id: 'src-norecord',
  registered: true,
  declared_state: 'active',
  declared_kind: 'scrape',
  endpoint_host: 'unmeasured.example',
  asserted: asserted({ admiralty_grade: 'A1', admiralty_rater: 'vendor' }),
  earned: null,
  computed: computed({ freshness_grade: 'empty', last_signal_at: null, signals_24h: 0, signals_7d: 0 }),
}

/** A row that EXISTS and has simply never been contested. Different absence. */
const NEVER_CONTESTED: SourceQualityRow = {
  source_id: 'src-uncontested',
  registered: true,
  declared_state: 'active',
  declared_kind: 'rss',
  endpoint_host: 'quiet.example',
  asserted: asserted(),
  earned: earned({
    wins: 0,
    losses: 0,
    contested_total: 0,
    win_rate_raw: null,
    win_rate_smoothed: 0.5,
    win_rate_lower: 0,
    low_sample: true,
    corroborated: 0,
    corroboration_total: 0,
    corroboration_rate: null,
  }),
  computed: computed(),
}

const DEBT: StalenessDebtResponse = {
  staleness_debt: 9,
  open_flags: 10,
  superseded_consumer_flags: 2,
  flagged_consumers: 4,
  moved_foundations: 3,
  closed_flags: 21,
  oldest_open_at: '2026-08-01T00:00:00Z',
  newest_open_at: '2026-08-20T00:00:00Z',
  by_reason: [
    { reason: 'foundation_superseded', open_flags: 6 },
    { reason: 'foundation_retired', open_flags: 4 },
  ],
  last_matcher_run_at: '2026-08-20T06:00:00Z',
  match_verified: false,
}

const DETAIL: SourceQualityDetail = {
  ...LOW_SAMPLE,
  includes_private: false,
  ratings: [
    {
      rating_id: 'rating-1',
      source_id: 'src-lowsample',
      rater: 'self',
      visibility_class: 'public',
      method: 'self_declared',
      admiralty_reliability: 'A',
      admiralty_credibility: '1',
      grade: 'A1',
      rubric: {},
      references: [{ url: 'https://thin.example/about' }],
      rated_at: '2026-07-01T00:00:00Z',
    },
  ],
  dossier: {
    dossier_id: 'dos-1',
    source_id: 'src-lowsample',
    dossier_md: '# Thin Example\n\nA wire service of unverified provenance.',
    references: [],
    compiled_by: 'analyst-9',
    compiled_at: '2026-07-02T00:00:00Z',
  },
}

// ---------------------------------------------------------------------------
// Fetch routing
// ---------------------------------------------------------------------------

interface MockOpts {
  rows?: SourceQualityRow[]
  debt?: StalenessDebtResponse
  detail?: SourceQualityDetail
  qualityResponse?: () => Response
  debtResponse?: () => Response
}

function mockFetch(opts: MockOpts = {}) {
  const rows = opts.rows ?? [MEASURED, LOW_SAMPLE, NO_RECORD, NEVER_CONTESTED]
  const fetchMock = vi.fn(async (url: string) => {
    const u = String(url)
    if (u.includes('/system/staleness-debt')) {
      if (opts.debtResponse) return opts.debtResponse()
      return { ok: true, json: async () => opts.debt ?? DEBT } as unknown as Response
    }
    // The drill-down route (`/v3/sources/{id}/quality`) is checked first — it
    // is a different path from the rollup (`/v3/source-quality`).
    if (/\/v3\/sources\/[^/]+\/quality/.test(u)) {
      return { ok: true, json: async () => opts.detail ?? DETAIL } as unknown as Response
    }
    if (u.includes('/v3/source-quality')) {
      if (opts.qualityResponse) return opts.qualityResponse()
      return { ok: true, json: async () => rows } as unknown as Response
    }
    return { ok: true, json: async () => ({}) } as unknown as Response
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

function renderPanel() {
  return render(wrap(<SourceHealthPanel registration={reg()} scope={{}} mode="personal" />))
}

// ---------------------------------------------------------------------------

describe('SourceHealthPanel — the rollup table', () => {
  it('renders one row per source off the BARE-ARRAY /v3/source-quality', async () => {
    const f = mockFetch()
    renderPanel()
    await screen.findByTestId('source-health-table')
    for (const id of ['src-measured', 'src-lowsample', 'src-norecord', 'src-uncontested']) {
      expect(screen.getByTestId(`source-health-row-${id}`)).toBeInTheDocument()
    }
    expect(f.mock.calls.some((c) => String(c[0]).includes('/v3/source-quality'))).toBe(true)
  })

  it('labels ASSERTED and EARNED as two separate column groups', async () => {
    mockFetch()
    renderPanel()
    const assertedHead = await screen.findByTestId('source-health-asserted-header')
    const earnedHead = screen.getByTestId('source-health-earned-header')
    expect(assertedHead).toHaveTextContent(/claimed, not evidence/i)
    expect(earnedHead).toHaveTextContent(/measured track record/i)
    expect(assertedHead).not.toBe(earnedHead)
  })

  it('renders an asserted grade as a CLAIM, and keeps it out of the earned cell', async () => {
    mockFetch()
    renderPanel()
    const grade = await screen.findByTestId('source-health-asserted-grade-src-lowsample')
    expect(grade).toHaveTextContent('claims A1')
    // The earned cell for the same source never mentions the asserted grade.
    expect(screen.getByTestId('source-health-earned-src-lowsample').textContent).not.toContain(
      'A1',
    )
  })

  it('shows the smoothed rate with its n, and demotes the flattering raw rate', async () => {
    mockFetch()
    renderPanel()
    const cell = await screen.findByTestId('source-health-winrate-src-lowsample')
    // Headline is the SMOOTHED rate, named as such, with n beside it.
    expect(screen.getByTestId('source-health-winrate-value-src-lowsample')).toHaveTextContent('60%')
    expect(cell.textContent).toContain('win_rate_smoothed')
    expect(cell.textContent).toContain('n=2')
    expect(cell.textContent).toContain('win_rate_lower')
    // The raw 100% still appears, but labelled and carrying the same n.
    expect(cell.textContent).toContain('win_rate_raw 100% n=2')
  })
})

describe('SourceHealthPanel — the honesty distinctions', () => {
  it('flags a low_sample source and shows the n the rate was computed over', async () => {
    mockFetch()
    renderPanel()
    const flag = await screen.findByTestId('source-health-low-sample-src-lowsample')
    expect(flag).toHaveTextContent(/low sample/i)
    expect(flag).toHaveTextContent('n=2')
    expect(flag).toHaveTextContent(/too few contests to mean anything/i)
    // …and the 200-contest source is NOT flagged.
    expect(screen.queryByTestId('source-health-low-sample-src-measured')).not.toBeInTheDocument()
  })

  it('renders earned:null differently from contested_total:0, and neither as a zero', async () => {
    mockFetch()
    renderPanel()
    await screen.findByTestId('source-health-table')

    const noRecord = screen.getByTestId('source-health-earned-state-src-norecord')
    const uncontested = screen.getByTestId('source-health-earned-state-src-uncontested')
    expect(noRecord).toHaveTextContent('no record')
    expect(uncontested).toHaveTextContent('never contested')
    expect(noRecord.textContent).not.toBe(uncontested.textContent)
    expect(noRecord.className).not.toBe(uncontested.className)

    // Neither win-rate cell shows a 0% that nobody measured.
    const noRecordRate = screen.getByTestId('source-health-winrate-src-norecord')
    expect(noRecordRate).toHaveTextContent(/no track-record row/i)
    expect(noRecordRate.textContent).not.toContain('0%')
    const uncontestedRate = screen.getByTestId('source-health-winrate-src-uncontested')
    expect(uncontestedRate).toHaveTextContent(/never contested/i)
    expect(uncontestedRate.textContent).not.toContain('0%')
  })

  it('renders a null corroboration_rate as an absence with its own denominator', async () => {
    mockFetch()
    renderPanel()
    const cell = await screen.findByTestId('source-health-corroboration-src-lowsample')
    expect(cell).toHaveTextContent('—')
    expect(cell).toHaveTextContent('n=2 corroboration checks')
    expect(cell).toHaveTextContent(/corroboration_rate is null — not computed/)
    expect(cell.textContent).not.toContain('0%')
  })

  it('does not paint `ungraded` / `empty` freshness like a fault', async () => {
    mockFetch()
    renderPanel()
    const ungraded = await screen.findByTestId('source-health-freshness-grade-src-lowsample')
    const empty = screen.getByTestId('source-health-freshness-grade-src-norecord')
    for (const pill of [ungraded, empty]) {
      expect(pill).toHaveTextContent(/absence, not a fault/)
      expect(pill.className).not.toMatch(/rose/)
      expect(pill.className).not.toMatch(/amber/)
    }
  })

  it('flags the asserted grade that no measured record backs', async () => {
    mockFetch()
    renderPanel()
    await screen.findByTestId('source-health-table')
    expect(
      screen.getByTestId('source-health-flag-src-norecord-asserted_unbacked'),
    ).toBeInTheDocument()
    expect(
      screen.getByTestId('source-health-flag-src-norecord-no_track_record'),
    ).toBeInTheDocument()
    // The measured source, which does not overclaim, carries neither.
    expect(
      screen.queryByTestId('source-health-flag-src-measured-asserted_unbacked'),
    ).not.toBeInTheDocument()
  })
})

describe('SourceHealthPanel — the staleness-debt strip', () => {
  it('renders the counts and the by_reason breakdown', async () => {
    mockFetch()
    renderPanel()
    const strip = await screen.findByTestId('source-health-debt')
    expect(strip).toHaveTextContent('staleness debt')
    expect(screen.getByTestId('source-health-stat-open_flags')).toHaveTextContent('10')
    expect(screen.getByTestId('source-health-stat-flagged_consumers')).toHaveTextContent('4')
    expect(screen.getByTestId('source-health-stat-moved_foundations')).toHaveTextContent('3')
    expect(screen.getByTestId('source-health-stat-closed_flags')).toHaveTextContent('21')

    const superseded = screen.getByTestId('source-health-debt-reason-foundation_superseded')
    expect(superseded).toHaveTextContent('6')
    expect(superseded).toHaveTextContent('60%')
    expect(
      screen.getByTestId('source-health-debt-reason-foundation_retired'),
    ).toHaveTextContent('4')
  })

  it('renders match_verified=false as a CAVEAT, never a green tick', async () => {
    mockFetch()
    renderPanel()
    const caveat = await screen.findByTestId('source-health-debt-caveat')
    expect(caveat).toHaveTextContent(/UNVERIFIED/)
    expect(caveat).toHaveTextContent(/lower bound/)
    expect(caveat.className).toMatch(/amber/)
    expect(caveat.className).not.toMatch(/emerald/)
  })

  it('says nothing at all when the server does verify the match', async () => {
    mockFetch({ debt: { ...DEBT, match_verified: true } })
    renderPanel()
    await screen.findByTestId('source-health-debt')
    expect(screen.queryByTestId('source-health-debt-caveat')).not.toBeInTheDocument()
  })

  it('surfaces a debt read failure without blanking the rest of the panel', async () => {
    mockFetch({ debtResponse: () => mockErrorResponse(500, { detail: 'pg down' }) })
    renderPanel()
    expect(await screen.findByTestId('source-health-debt-error')).toHaveTextContent(/pg down/)
    expect(await screen.findByTestId('source-health-table')).toBeInTheDocument()
  })
})

describe('SourceHealthPanel — load faults', () => {
  it('renders a 503 as "the view is not provisioned", not a generic error', async () => {
    mockFetch({
      qualityResponse: () =>
        mockErrorResponse(503, { detail: 'source_quality view missing (migration 0115)' }),
    })
    renderPanel()
    const state = await screen.findByTestId('source-health-not-provisioned')
    expect(state).toHaveTextContent(/not provisioned/i)
    expect(state).toHaveTextContent(/migration 0115/)
    expect(state).toHaveTextContent(/no source has been judged either way/i)
    // Explicitly NOT the generic error path.
    expect(screen.queryByTestId('source-health-error')).not.toBeInTheDocument()
    expect(screen.queryByTestId('source-health-table')).not.toBeInTheDocument()
  })

  it('renders any other failure as a plain error, distinct from the 503 state', async () => {
    mockFetch({ qualityResponse: () => mockErrorResponse(500, { detail: 'boom' }) })
    renderPanel()
    expect(await screen.findByTestId('source-health-error')).toHaveTextContent(/boom/)
    expect(screen.queryByTestId('source-health-not-provisioned')).not.toBeInTheDocument()
  })

  it('renders an honest empty state when the view returns no rows', async () => {
    mockFetch({ rows: [] })
    renderPanel()
    expect(await screen.findByTestId('source-health-empty')).toHaveTextContent(
      /returned no rows/i,
    )
  })
})

describe('SourceHealthPanel — the per-source drill-down', () => {
  it('fetches the detail ONLY for the row the operator opens', async () => {
    const f = mockFetch()
    renderPanel()
    await screen.findByTestId('source-health-table')

    const detailCalls = () =>
      f.mock.calls.filter((c) => /\/v3\/sources\/[^/]+\/quality/.test(String(c[0])))
    expect(detailCalls()).toHaveLength(0)

    fireEvent.click(screen.getByTestId('source-health-toggle-src-lowsample'))
    await waitFor(() => expect(detailCalls().length).toBeGreaterThan(0))
    expect(String(detailCalls()[0][0])).toContain('/v3/sources/src-lowsample/quality')
    // Still nothing requested for any other source.
    expect(
      detailCalls().every((c) => String(c[0]).includes('src-lowsample')),
    ).toBe(true)
  })

  it('renders the ratings and the dossier as CLAIMS, beside the measured record', async () => {
    mockFetch()
    renderPanel()
    await screen.findByTestId('source-health-table')
    fireEvent.click(screen.getByTestId('source-health-toggle-src-lowsample'))

    const rating = await screen.findByTestId('source-health-rating-rating-1')
    expect(rating).toHaveTextContent('claims A1')
    expect(rating).toHaveTextContent('by self')
    expect(rating).toHaveTextContent('1 reference')

    const dossier = screen.getByTestId('source-health-dossier-src-lowsample')
    expect(dossier).toHaveTextContent('A wire service of unverified provenance.')
    expect(dossier).toHaveTextContent('analyst-9')

    // The two halves are still two halves inside the drill-down.
    expect(screen.getByTestId('source-health-detail-asserted-src-lowsample')).toHaveTextContent(
      /claims A1/,
    )
    expect(screen.getByTestId('source-health-detail-earned-src-lowsample')).toHaveTextContent(
      /too few to mean anything/,
    )
  })

  it('states the tension when the claim outruns the record', async () => {
    mockFetch()
    renderPanel()
    await screen.findByTestId('source-health-table')
    fireEvent.click(screen.getByTestId('source-health-toggle-src-norecord'))

    expect(await screen.findByTestId('source-health-tension-src-norecord')).toHaveTextContent(
      /Asserts A1 with NO measured contest record/,
    )
  })

  it('surfaces a drill-down failure on the opened row only', async () => {
    const f = vi.fn(async (url: string) => {
      const u = String(url)
      if (u.includes('/system/staleness-debt')) {
        return { ok: true, json: async () => DEBT } as unknown as Response
      }
      if (/\/v3\/sources\/[^/]+\/quality/.test(u)) {
        return mockErrorResponse(404, { detail: 'source not found' })
      }
      return { ok: true, json: async () => [MEASURED] } as unknown as Response
    })
    vi.stubGlobal('fetch', f)
    renderPanel()
    await screen.findByTestId('source-health-table')
    fireEvent.click(screen.getByTestId('source-health-toggle-src-measured'))

    expect(await screen.findByTestId('source-health-detail-error-src-measured')).toHaveTextContent(
      /source not found/,
    )
  })

  it('collapses the row again on a second click', async () => {
    mockFetch()
    renderPanel()
    await screen.findByTestId('source-health-table')
    fireEvent.click(screen.getByTestId('source-health-toggle-src-measured'))
    expect(await screen.findByTestId('source-health-detail-src-measured')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('source-health-toggle-src-measured'))
    await waitFor(() =>
      expect(screen.queryByTestId('source-health-detail-src-measured')).not.toBeInTheDocument(),
    )
  })
})

describe('SourceHealthPanel — controls', () => {
  it('re-queries with contested_only when the chip is toggled', async () => {
    const f = mockFetch()
    renderPanel()
    await screen.findByTestId('source-health-table')
    fireEvent.click(screen.getByTestId('source-health-contested-only'))
    await waitFor(() => {
      expect(f.mock.calls.some((c) => String(c[0]).includes('contested_only=true'))).toBe(true)
    })
  })

  it('reorders the table when the sort changes', async () => {
    mockFetch()
    renderPanel()
    await screen.findByTestId('source-health-table')

    const ids = () =>
      screen
        .getAllByTestId(/^source-health-row-/)
        .map((el) => el.getAttribute('data-testid'))

    // Default: attention — the unbacked A1 claims lead, the clean row trails.
    expect(ids()[ids().length - 1]).toBe('source-health-row-src-measured')

    fireEvent.click(screen.getByTestId('source-health-sort-source'))
    await waitFor(() => {
      expect(ids()).toEqual([
        'source-health-row-src-lowsample',
        'source-health-row-src-measured',
        'source-health-row-src-norecord',
        'source-health-row-src-uncontested',
      ])
    })
  })
})
