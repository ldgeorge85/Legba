/**
 * subject.page on a SECOND profile (UI-B3): the sanctions skeleton's real
 * export (`test/profileNone.fixture.json` — no top floor, `geometry: none`,
 * no composing frame, five questions as its units), mocked in as the build's
 * `profile_ui.json`. A regime subject renders a brief over ITS roster — the
 * profile's question set — in the profile's words, with the absence contract;
 * none of the exemplar's words render.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

vi.mock('@/lib/profile_ui.json', async () => ({
  default: (await import('@/test/profileNone.fixture.json')).default,
}))

import SubjectPage from './SubjectPage'
import { PANEL_REGISTRY } from '@/panel-registry/registry'
import { compositionProducers, subjectRoster } from '@/lib/subjectPage'
import { setSubjectIndex, subjectIndexFrom } from '@/lib/subjectIndex'
import { resetAmbientPins } from '@/lib/consultContext'
import fixture from '@/test/profileNone.fixture.json'
import type { PanelRegistration } from '@/types'

const SANCTIONS_ROSTER = subjectIndexFrom([
  { descriptor_id: 'regime_ofac_russia', name: 'Regime — OFAC Russia', state: 'active', body: { scope: { domain: 'thematic', tags: ['regime'] } } },
  { descriptor_id: 'counterparty_acme', name: 'Counterparty — Acme Shipping', state: 'active', body: { scope: { tags: ['counterparty'] } } },
])

/** The exemplar's words — none may render on another profile's page. */
const EXEMPLAR = /\b(desk|Desk|G20|Watch list|country_composition|Countr(y|ies))\b/

function registration(subject: string): PanelRegistration {
  return {
    id: `subject.page:${subject}`,
    panel_id: 'subject_page',
    descriptor_id: subject,
    descriptor_version: '00000000',
    descriptor_family: 'target',
    analyst_id: null,
    title: subject,
    mode: 'personal',
    layout_slot: 'subject.page',
    data_query: {},
    binding: { target_id: subject },
    retired: false,
    created_at: new Date().toISOString(),
    retired_at: null,
  } as unknown as PanelRegistration
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

beforeEach(() => {
  setSubjectIndex(SANCTIONS_ROSTER)
  resetAmbientPins()
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (url.includes('/correctness')) {
        return { ok: true, json: async () => ({ reference: { state: 'none' }, data: [], next_cursor: null }) }
      }
      if (url.includes('/v3/layers/divergence')) {
        return { ok: true, json: async () => ({ measured: true, desks: [], desks_unresolved: [], warnings: [] }) }
      }
      return { ok: true, json: async () => ({ data: [], next_cursor: null }) }
    }),
  )
})

describe('subject.page on the sanctions profile — a regime', () => {
  it('the kind is titled with the profile\'s subject noun', () => {
    expect(PANEL_REGISTRY['subject.page'].definition.defaultTitle).toBe('Regime')
  })

  it('a regime\'s roster is the profile\'s question set — every unscoped question (no leg is set apart)', () => {
    expect(compositionProducers()).toEqual([])
    const r = subjectRoster('regime_ofac_russia')
    expect(r.basis).toBe('every')
    const declared = (fixture as { rosters: { units: { id: string; scope_tag?: string }[] } }).rosters.units
    expect(r.units.map((u) => u.id)).toEqual(declared.filter((u) => !u.scope_tag || u.scope_tag === 'regime').map((u) => u.id))
    for (const u of r.units) expect(u.question, u.id).toBeTruthy()
  })

  it('renders the brief over its own roster in the profile\'s words, with the absence contract', async () => {
    render(wrap(<SubjectPage registration={registration('regime_ofac_russia')} scope={{ target_id: 'regime_ofac_russia' }} mode="personal" />))
    expect(screen.getByTestId('subject-page')).toHaveTextContent('Regime')
    expect(screen.getByTestId('subject-page-name')).toHaveTextContent('OFAC Russia')
    expect(await screen.findByTestId('desk-brief-no-composition-headline', {}, { timeout: 15000 })).toHaveTextContent(
      'No composition on record for this regime.',
    )
    expect(screen.getByTestId('desk-brief-no-composition')).toHaveTextContent('declares no composition producer')
    const r = subjectRoster('regime_ofac_russia')
    expect(screen.getByTestId('desk-brief-units-carried')).toHaveTextContent(`0 of ${r.units.length} on the roster`)
    for (const u of r.units) {
      const row = screen.getByTestId(`desk-brief-evidence-row-${u.id}`)
      // a unit with no read → no read yet; its standing question beside it
      expect(row).toHaveAttribute('data-state', 'no_read')
      expect(row).toHaveTextContent(u.question!)
    }
    await waitFor(() => expect(screen.getByTestId('desk-brief-layers')).toHaveTextContent('No layer map is declared for this regime'))
    const text = screen.getByTestId('subject-page').textContent ?? ''
    const hit = text.match(EXEMPLAR)
    expect(hit ? text.slice(Math.max(0, hit.index! - 60), hit.index! + 40) : null).toBeNull()
  })
})
