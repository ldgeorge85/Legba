/**
 * subject.page — the Brief tab over the subject's OWN roster (UI-B3), on the
 * geopolitics exemplar's real profile and subject roster fixture, with the
 * REAL brief component (no stubs) and one routed fetch mock:
 *
 *   * a country desk — the exemplar's words byte-identical ("Desk", "desk
 *     brief", the scored units' labels), its roster = the scored subjects'
 *     units (the lane unit is not on it);
 *   * a supply-chain lane (`lane_hormuz`) — its OWN roster (the one lane unit),
 *     its declared name (never an ISO guess), no composition → the absence
 *     line, and its unit's own latest read standing in for the record;
 *   * a region — no unit fans out over it: the roster's absence stated by
 *     name, never a country-shaped empty table.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import type { PanelRegistration } from '@/types'
import SubjectPage from './SubjectPage'
import { subjectUnits } from '@/lib/profileUi'
import { subjectRoster } from '@/lib/subjectPage'
import { resetAmbientPins } from '@/lib/consultContext'
import { resetUnitCorrectnessCache } from '@/lib/unitCorrectnessModel'

const FRESH = new Date(Date.now() - 2 * 3600_000).toISOString()

function registration(subject: string): PanelRegistration {
  return {
    id: `subject.page:${subject}`,
    panel_id: 'subject_page',
    descriptor_id: subject,
    descriptor_version: '00000000',
    descriptor_family: 'target',
    analyst_id: null,
    title: `Desk · ${subject}`,
    mode: 'personal',
    layout_slot: 'subject.page',
    data_query: {},
    binding: { target_id: subject },
    retired: false,
    created_at: FRESH,
    retired_at: null,
  } as unknown as PanelRegistration
}

/** Every read empty, except the lane unit's own latest read on the lane. */
const requested: string[] = []
function routedFetch() {
  return vi.fn(async (url: string) => {
    requested.push(url)
    if (url.includes('/correctness')) {
      return { ok: true, json: async () => ({ reference: { state: 'none' }, data: [], next_cursor: null }) }
    }
    if (url.includes('country_scorecard')) return { ok: true, json: async () => [] }
    if (url.includes('/v3/layers/divergence')) {
      return { ok: true, json: async () => ({ measured: true, desks: [], desks_unresolved: [], warnings: [] }) }
    }
    if (url.includes('target_id=lane_hormuz') && url.includes('analyst_id_in=disruption_status')) {
      return {
        ok: true,
        json: async () => ({
          data: [{ id: 'ds-1', analyst_id: 'disruption_status', target_id: 'lane_hormuz', produced_at: FRESH, severity: 'low' }],
          next_cursor: null,
        }),
      }
    }
    return { ok: true, json: async () => ({ data: [], next_cursor: null }) }
  })
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

function renderPage(subject: string) {
  return render(
    wrap(<SubjectPage registration={registration(subject)} scope={{ target_id: subject }} mode="personal" />),
  )
}

beforeEach(() => {
  requested.length = 0
  vi.stubGlobal('fetch', routedFetch())
  resetAmbientPins()
  resetUnitCorrectnessCache()
})

describe('subject.page#brief — a country desk (the exemplar, byte-identical labels)', () => {
  it('reads as the desk brief it was: Desk, desk brief, the scored units by their profile labels', async () => {
    renderPage('country_g20_br')
    expect(screen.getByTestId('subject-page')).toHaveTextContent('Desk')
    expect(screen.getByTestId('subject-page-name')).toHaveTextContent('Brazil')
    const card = await screen.findByTestId('desk-brief-index-card', {}, { timeout: 15000 })
    expect(card).toHaveTextContent('desk brief · country_g20_br')
    expect(await screen.findByTestId('desk-brief-no-composition-headline')).toHaveTextContent(
      'No composition on record for this desk.',
    )
    const roster = subjectUnits()
    expect(screen.getByTestId('desk-brief-units-carried')).toHaveTextContent(`0 of ${roster.length} on the roster`)
    for (const u of roster) {
      expect(screen.getByTestId(`desk-brief-evidence-row-${u.id}`)).toHaveTextContent(u.label)
    }
    // The lane unit fans out over the supply-chain leg only.
    expect(screen.queryByTestId('desk-brief-evidence-row-disruption_status')).toBeNull()
    expect(screen.getByTestId('desk-brief-layers')).toHaveTextContent('source layers for this desk')
  })

  it('asks every composing frame of the profile for the composition, in one read', async () => {
    renderPage('country_g20_br')
    await screen.findByTestId('desk-brief-no-composition')
    const ask = requested.find((u) => u.includes('target_id=country_g20_br') && u.includes('analyst_id_in='))
    expect(ask).toBeDefined()
    expect(decodeURIComponent(ask!)).toContain('country_composition')
    expect(decodeURIComponent(ask!)).toContain('region_composition')
  })
})

describe('subject.page#brief — a supply-chain lane renders a brief over its OWN roster', () => {
  it('its roster is the lane unit alone — no country desk\'s nine', () => {
    const r = subjectRoster('lane_hormuz')
    expect(r.basis).toBe('leg')
    expect(r.units.map((u) => u.id)).toEqual(['disruption_status'])
  })

  it('names the lane as it declares itself, states the missing composition, and reads its unit', async () => {
    renderPage('lane_hormuz')
    const name = screen.getByTestId('subject-page-name')
    expect(name).toHaveTextContent('Strait of Hormuz')
    // No ISO guess: the lane declares seven geo codes and is not a country.
    expect(name.textContent).not.toMatch(/Iran|Oman|Qatar|Kuwait|Iraq|Bahrain|Emirates/)
    expect(await screen.findByTestId('desk-brief-no-composition-headline')).toHaveTextContent(
      'No composition on record for this desk.',
    )
    expect(screen.getByTestId('desk-brief-units-carried')).toHaveTextContent('0 of 1 on the roster')
    const rows = within(screen.getByTestId('desk-brief-evidence')).getAllByTestId(/^desk-brief-evidence-row-/)
    expect(rows.map((r) => r.getAttribute('data-testid'))).toEqual(['desk-brief-evidence-row-disruption_status'])
    // The unit's own latest read stands in for the record that does not exist.
    await waitFor(() =>
      expect(screen.getByTestId('desk-brief-evidence-row-disruption_status')).toHaveAttribute('data-state', 'in_basis'),
    )
    expect(screen.getByTestId('desk-brief-evidence-row-disruption_status')).toHaveTextContent('Disruption status')
    // Correctness absent → unmeasured beside its coverage, never 0.
    expect(screen.getByTestId('desk-brief-evidence-row-disruption_status')).toHaveTextContent('unmeasured')
    expect(screen.getByTestId('desk-brief-evidence-row-disruption_status')).not.toHaveTextContent('0%')
    // A layer not declared → "no layer map is declared".
    expect(screen.getByTestId('desk-brief-layers')).toHaveTextContent('No layer map is declared')
  })
})

describe('subject.page#brief — a subject no unit fans out over', () => {
  it('states the empty roster by name, never as a country-shaped empty table', async () => {
    renderPage('region_africa')
    expect(await screen.findByTestId('desk-brief-roster-reason')).toHaveTextContent(
      "No roster: no unit fans out over this desk's tags",
    )
    expect(screen.queryAllByTestId(/^desk-brief-evidence-row-/)).toEqual([])
  })

  it('a subject the registry has not served has NO roster yet — none is assumed', async () => {
    renderPage('lane_unknown_xyz')
    expect(await screen.findByTestId('desk-brief-roster-reason')).toHaveTextContent('has not been read')
    expect(screen.queryAllByTestId(/^desk-brief-evidence-row-/)).toEqual([])
  })
})
