/**
 * ReportsList (UI-B2) — the phone navigator's tiers and roster legs read the
 * PROFILE, never a literal: the tier headings and producers are the profile's
 * frames (or the projection's `tiers` handed in), and the per-subject tier's
 * reads sub-group by the roster leg each subject DECLARES (its `scope.tags`
 * against the profile's `rosters.subject_tags`), under the profile's roster
 * label — byte for byte today's "G20" / "Watch list" under geopolitics.
 */
import { describe, expect, it } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import ReportsList from './ReportsList'
import type { ReportRow } from '../mobileModel'
import raw from '@/lib/profile_ui.json'
import { subjectIndexFrom } from '@/lib/subjectIndex'

function row(id: string, tier: string, targetId: string | null, target: string): ReportRow {
  return {
    id,
    tier,
    target,
    targetId,
    producedAt: '2026-10-04T06:00:00Z',
    day: '2026-10-04',
    title: id,
    leadThread: 'lead',
    analystId: null,
    regionHint: null,
  }
}

const ROWS = [
  row('w', 'world', null, 'World'),
  row('br', 'country', 'country_g20_br', 'Brazil'),
  row('sd', 'country', 'country_watch_sd', 'Sudan'),
  row('xx', 'country', 'some_unregistered_subject', 'Somewhere'),
]

function list(extra: Partial<Parameters<typeof ReportsList>[0]> = {}) {
  return render(
    <ReportsList
      rows={ROWS}
      day={null}
      days={['2026-10-04']}
      onOpen={() => {}}
      onDay={() => {}}
      consolidation={null}
      onOpenConsolidation={() => {}}
      offlineNote={null}
      {...extra}
    />,
  )
}

describe('geopolitics: today\'s headings, byte for byte, from the profile', () => {
  it('tier headings are the profile frames\' labels; the per-subject tier groups by declared leg', () => {
    list()
    expect(screen.getByTestId('tier-group-world').querySelector('h2')!.textContent).toContain(
      raw.frames.find((f) => f.id === 'world')!.label,
    )
    const country = screen.getByTestId('tier-group-country')
    expect(country.querySelector('h2')!.textContent).toContain('Country')
    expect(within(screen.getByTestId('roster-group-g20')).getByText('Brazil')).toBeInTheDocument()
    expect(screen.getByTestId('roster-group-g20').querySelector('h3')!.textContent).toContain(raw.labels.roster.g20)
    expect(screen.getByTestId('roster-group-watch').querySelector('h3')!.textContent).toContain(raw.labels.roster.watch)
    // a subject on no declared leg is listed, never dropped
    expect(within(screen.getByTestId('roster-group-other')).getByText('Somewhere')).toBeInTheDocument()
    // the row meta reads the tier label too
    expect(within(country).getAllByText(/^Country · 2026-10-04$/).length).toBe(3)
  })

  it('a roster handed in decides the leg — the declaration, not the id', () => {
    const subjects = subjectIndexFrom([
      { descriptor_id: 'country_g20_br', name: 'G20 — Brazil', body: { scope: { domain: 'geo', geo: ['BR'], tags: ['watch'] } } },
    ])
    list({ subjects })
    expect(within(screen.getByTestId('roster-group-watch')).getByText('Brazil')).toBeInTheDocument()
    expect(screen.queryByTestId('roster-group-g20')).toBeNull()
  })
})

describe('the projection\'s tiers, when handed in, decide the headings', () => {
  it('another plane\'s tier set renders its own labels and nests its own per-subject tier', () => {
    const tiers = [
      { id: 'world', label: 'Instance', parent: null, producer: 'world_assessor' },
      { id: 'country', label: 'Member', parent: 'world', producer: 'country_composition' },
    ]
    list({ tiers })
    expect(screen.getByTestId('tier-group-world').querySelector('h2')!.textContent).toContain('Instance')
    expect(screen.getByTestId('tier-group-country').querySelector('h2')!.textContent).toContain('Member')
    expect(screen.getByTestId('roster-group-g20')).toBeInTheDocument()
  })
})
