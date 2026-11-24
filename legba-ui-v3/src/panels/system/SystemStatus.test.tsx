/**
 * Component test for System Status — Acquisition section.
 *
 * Asserts the A7 `freshness_grade` column renders per source (grade text +
 * a color-coded dot whose title carries the honest cadence-budget context),
 * alongside the existing `status` column — never replacing it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import SystemStatusPanel from './SystemStatus'
import type { PanelRegistration } from '@/types'

function reg(): PanelRegistration {
  return {
    id: 'p',
    panel_id: 'system_status',
    descriptor_id: 'system.status',
    descriptor_version: 'v' + 'a'.repeat(63),
    descriptor_family: 'analyst',
    analyst_id: null,
    title: 'System Status',
    mode: 'personal',
    layout_slot: 'system.status.main',
    data_query: {},
    binding: {},
    retired: false,
    created_at: '2026-05-20T00:00:00Z',
    retired_at: null,
  }
}
function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

const SOURCES = [
  {
    source_id: 'gdelt',
    state: 'active',
    signals_24h: 40,
    signals_7d: 300,
    last_seen_at: '2026-07-27T00:00:00Z',
    age_seconds: 120,
    last_poll_outcome: null,
    recent_error_count: 0,
    status: 'firing',
    freshness_grade: 'ok',
    budget_minutes: 45,
  },
  {
    source_id: 'usgs',
    state: 'active',
    signals_24h: 0,
    signals_7d: 0,
    last_seen_at: null,
    age_seconds: null,
    last_poll_outcome: null,
    recent_error_count: 0,
    status: 'silent',
    freshness_grade: 'empty',
    budget_minutes: 30,
  },
  {
    source_id: 'stale-feed',
    state: 'active',
    signals_24h: 0,
    signals_7d: 2,
    last_seen_at: '2026-07-25T00:00:00Z',
    age_seconds: 172_800,
    last_poll_outcome: null,
    recent_error_count: 0,
    status: 'silent',
    freshness_grade: 'warn',
    budget_minutes: 60,
  },
]

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('SystemStatusPanel — Acquisition freshness column', () => {
  it('renders each source freshness_grade alongside its status', async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url.includes('/v3/system/source-firing')) {
        return Promise.resolve({ ok: true, json: async () => SOURCES })
      }
      return Promise.resolve({ ok: true, json: async () => [] })
    })
    vi.stubGlobal('fetch', fetchMock)
    render(wrap(<SystemStatusPanel registration={reg()} scope={{}} mode="personal" />))

    await waitFor(() => expect(screen.getByTestId('status-source-gdelt')).toBeInTheDocument())

    expect(screen.getByTestId('status-source-freshness-gdelt')).toHaveTextContent('ok')
    expect(screen.getByTestId('status-source-freshness-usgs')).toHaveTextContent('empty')
    expect(screen.getByTestId('status-source-freshness-stale-feed')).toHaveTextContent('warn')

    // The honest cadence-budget context lands in the title, not just the grade word.
    const gdeltCell = screen.getByTestId('status-source-freshness-gdelt').querySelector('span')
    expect(gdeltCell?.getAttribute('title')).toMatch(/budget 45m/)

    // The existing status pill is untouched — freshness is an ADDITIONAL column.
    expect(screen.getByTestId('status-source-pill-firing')).toBeInTheDocument()
  })
})

describe('SystemStatusPanel — Infra backup line (X1-A2)', () => {
  function stub(backup: unknown) {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url.includes('/v3/system/backup-status')) {
        return Promise.resolve({ ok: true, json: async () => backup })
      }
      return Promise.resolve({ ok: true, json: async () => [] })
    })
    vi.stubGlobal('fetch', fetchMock)
    return fetchMock
  }

  it('shows the last backup as an age with its UTC stamp, and an absent drill as "never recorded"', async () => {
    const at = new Date(Date.now() - 2 * 3600_000).toISOString()
    const fetchMock = stub({ last_backup_at: at, last_drill_at: null })
    render(wrap(<SystemStatusPanel registration={reg()} scope={{}} mode="personal" />))

    await waitFor(() =>
      expect(screen.getByTestId('status-backup-last')).toHaveTextContent('2.0h ago'),
    )
    expect(screen.getByTestId('status-backup-last').getAttribute('title')).toBe(`${at} (UTC)`)
    expect(screen.getByTestId('status-backup-last').className).toMatch(/text-ink-1/)
    expect(screen.getByTestId('status-backup-drill')).toHaveTextContent('never recorded')
    expect(
      fetchMock.mock.calls.some(([u]) => String(u).includes('/v3/system/backup-status')),
    ).toBe(true)
  })

  it('never renders a time when nothing is recorded, and goes amber past the nightly cadence', async () => {
    stub({ last_backup_at: null, last_drill_at: null })
    const { unmount } = render(
      wrap(<SystemStatusPanel registration={reg()} scope={{}} mode="personal" />),
    )
    await waitFor(() =>
      expect(screen.getByTestId('status-backup-last')).toHaveTextContent('never recorded'),
    )
    expect(screen.getByTestId('status-backup')).not.toHaveTextContent(/ago/)
    unmount()

    stub({ last_backup_at: new Date(Date.now() - 3 * 86400_000).toISOString(), last_drill_at: null })
    render(wrap(<SystemStatusPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() =>
      expect(screen.getByTestId('status-backup-last')).toHaveTextContent('3.0d ago'),
    )
    expect(screen.getByTestId('status-backup-last').className).toMatch(/amber/)
  })
})
