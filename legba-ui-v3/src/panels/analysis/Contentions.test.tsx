/**
 * W2 S-batch — the contentions LISTING (`analysis.contentions`).
 *
 * Asserts the panel renders the route's rows with their stance, host class and
 * fences; that an unmeasured fence reads "not measured" (never 0); that the
 * route's contract note and its scope_field are shown; that the filters are
 * the route's own parameters; and that a row with a target opens Cross-framing
 * on its exact claim id.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

import ContentionsPanel from './Contentions'
import {
  contentionsListPath,
  scopeFieldLine,
  stanceLabel,
  type ContentionRow,
  type ContentionsResponse,
} from '@/lib/contentionsModel'
import { crossFramingSubjectFromParams } from '@/lib/crossFramingLink'
import { pendingPanelParams, resetPanelParams } from '@/state/panelParams'
import type { PanelRegistration } from '@/types'

function reg(): PanelRegistration {
  return {
    id: 'p',
    panel_id: 'analysis_contentions',
    descriptor_id: '(singleton)',
    descriptor_version: '0',
    descriptor_family: 'analyst',
    analyst_id: null,
    title: 'Contentions',
    mode: 'personal',
    layout_slot: 'analysis.contentions.main',
    data_query: {},
    binding: {},
    retired: false,
    created_at: '2026-10-03T00:00:00Z',
    retired_at: null,
  }
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

function row(over: Partial<ContentionRow> & { claim_id: string }): ContentionRow {
  return {
    claim_text: 'Fuel imports fell sharply in September',
    finding_id: 'ffffffff-0000-4000-8000-000000000001',
    origin_head_id: null,
    block_ordinal: 1,
    span_role: 'claim',
    target_id: 'country_ruritania',
    desk_key: 'country_ruritania',
    analyst_id: 'contrary_evidence',
    query: 'fuel imports rose September',
    query_source: 'llm',
    query_novel_tokens: 2,
    stance: 'contradicts',
    derivation: 'polarity',
    reason: '',
    statement: 'A page this platform holds states the opposite.',
    rung: 'free',
    refs: [
      {
        url: 'https://example.org/a',
        sha256: 'ab',
        chars: 100,
        published_at: '2026-09-30',
        extracted: true,
        fetched_at: '2026-10-02T00:00:00Z',
        status_code: 200,
        stance: 'contradicts',
        quote: 'imports rose',
        host_class: 'reporting',
        page_published_at: '2026-09-30',
        subject_overlap: 3,
        fence: '',
      },
    ],
    linked_signals: 0,
    host_class: 'reporting',
    page_published_at: '2026-09-30',
    subject_overlap: 3,
    independent_pages: 2,
    pipeline_version: 'p1',
    retrieved_at: '2026-10-02T00:00:00Z',
    as_of: '2026-10-02',
    expires_at: '2026-10-09T00:00:00Z',
    live: true,
    ...over,
  }
}

const RESPONSE: ContentionsResponse = {
  version: '2026-09/7a.2',
  note: 'Each row is a RETRIEVAL outcome against one published claim, never a verdict on it.',
  scope: null,
  scope_field: null,
  stances: ['contradicts', 'qualifies', 'none_found', 'search_failed'],
  contentions: [
    row({ claim_id: 'c1' }),
    row({
      claim_id: 'c2',
      stance: 'none_found',
      target_id: null,
      desk_key: 'energy_security',
      live: false,
      refs: [],
      host_class: null,
      page_published_at: null,
      subject_overlap: null,
      independent_pages: null,
      statement: '',
    }),
  ],
}

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => RESPONSE })
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
  resetPanelParams()
})

describe('contentions listing — model helpers', () => {
  it('sends only the filters asked for', () => {
    expect(contentionsListPath()).toBe('/v3/contentions?limit=100')
    expect(
      contentionsListPath({ scope: ' country_g20_in ', stance: 'qualifies', liveOnly: true }),
    ).toBe('/v3/contentions?scope=country_g20_in&stance=qualifies&live_only=true&limit=100')
    expect(contentionsListPath({ scope: '   ', stance: null })).toBe('/v3/contentions?limit=100')
  })

  it('names a stance as a retrieval, never a verdict', () => {
    expect(stanceLabel('contradicts')).toBe('counter-page states the opposite')
    expect(stanceLabel('none_found')).toBe('looked, found nothing')
    expect(stanceLabel('contradicts')).not.toMatch(/false|wrong/i)
  })

  it('says which handle the scope matched on, and when it matched none', () => {
    expect(scopeFieldLine(null)).toBeNull()
    expect(scopeFieldLine({ ...RESPONSE, scope: 'x', scope_field: 'desk_key' })).toContain('as a desk key')
    expect(scopeFieldLine({ ...RESPONSE, scope: 'x', scope_field: null })).toContain('nothing matched')
  })
})

describe('ContentionsPanel', () => {
  it('lists every record with its stance, host class and fences, and the route’s note', async () => {
    render(wrap(<ContentionsPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('contention-row-c1')).toBeInTheDocument())
    expect(String(fetchMock.mock.calls[0][0])).toContain('/v3/contentions?limit=100')
    expect(screen.getByTestId('contentions-note').textContent).toContain('never a verdict')

    const first = screen.getByTestId('contention-row-c1')
    expect(within(first).getByTestId('contention-stance').textContent).toBe(
      'counter-page states the opposite',
    )
    const fences = within(first).getByTestId('contention-fences').textContent ?? ''
    expect(fences).toContain('reporting')
    expect(fences).toContain('2026-09-30')
    expect(fences).toContain("3 of the claim's subject words")
    expect(fences).toContain('2 of 2 independent pages')
  })

  it('prints an unmeasured fence as absence, never 0, and an expired row as expired', async () => {
    render(wrap(<ContentionsPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('contention-row-c2')).toBeInTheDocument())
    const second = screen.getByTestId('contention-row-c2')
    const fences = within(second).getByTestId('contention-fences').textContent ?? ''
    expect(fences).toContain('not measured')
    expect(fences).not.toMatch(/\b0\b/)
    expect(within(second).getByTestId('contention-shelf').textContent).toBe(
      'expired, not re-checked',
    )
    // No target → no cross-framing action (the panel never guesses a desk).
    expect(screen.queryByTestId('contention-cross-framing-c2')).toBeNull()
  })

  it('the filters are the route’s own parameters', async () => {
    render(wrap(<ContentionsPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('contention-row-c1')).toBeInTheDocument())
    fireEvent.change(screen.getByTestId('contentions-stance'), { target: { value: 'qualifies' } })
    fireEvent.click(screen.getByTestId('contentions-live-only'))
    fireEvent.change(screen.getByTestId('contentions-scope'), { target: { value: 'energy_security' } })
    fireEvent.submit(screen.getByTestId('contentions-scope').closest('form')!)
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some((c) =>
          String(c[0]).includes(
            '/v3/contentions?scope=energy_security&stance=qualifies&live_only=true&limit=100',
          ),
        ),
      ).toBe(true),
    )
  })

  it('a row with a target opens Cross-framing on its exact claim id', async () => {
    render(wrap(<ContentionsPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('contention-cross-framing-c1')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('contention-cross-framing-c1'))
    // The open_panel's params carry the claim (UI-B1/shell retired the park).
    const req = pendingPanelParams('analysis.cross_framing')
    expect(req && crossFramingSubjectFromParams(req.params)).toEqual({
      targetId: 'country_ruritania',
      claimId: 'c1',
      claimText: 'Fuel imports fell sharply in September',
      origin: 'analysis.contentions',
    })
  })

  it('an empty answer says what it does not mean', async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ ...RESPONSE, contentions: [] }),
    })
    render(wrap(<ContentionsPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('contentions-empty')).toBeInTheDocument())
    expect(screen.getByTestId('contentions-empty').textContent).toContain(
      'not a claim that nothing is contested',
    )
  })
})
