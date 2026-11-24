/**
 * MorningRead (W4) — the verify arms line against the REAL verification shape.
 *
 * The server writes `verification.branch_scores.assembly = {score, checkable,
 * supported}` (+ `counters.assembly_quote_spans_*`), never `assembly_arms`. The
 * desktop page's "Quote fidelity ... by construction" line used to read the
 * latter, so it showed "not yet independently audited" on every live row.
 * Fixture: the sanitised real world_assessor e30e67d3 row.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import MorningRead from './MorningRead'
import spineFixture from '@/mobile/__fixtures__/world_spine.e30e67d3.json'
import { __resetReadLayout } from '@/lib/readLayout'
import { __resetReadTelemetry } from '@/lib/readTelemetry'
import { resetScope } from '@/state/scope'

type Row = Record<string, unknown>
const SPINE = spineFixture as unknown as Row

function stubFetch(rows: Row[]) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      const analyst = /analyst_id=([^&]+)/.exec(String(url))?.[1] ?? ''
      return {
        ok: true,
        status: 200,
        json: async () => ({ data: analyst === 'world_assessor' ? rows : [] }),
        text: async () => '',
      } as unknown as Response
    }),
  )
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

beforeEach(() => {
  __resetReadLayout()
  __resetReadTelemetry()
  resetScope()
  sessionStorage.clear()
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('the record badge on a row carrying branch_scores.assembly', () => {
  it('prints "k of n spans fully checked" when some spans were checked for containment only', async () => {
    // Lane N-D: the fixture's counters read ok 16 / checked 16 with
    // sha_unverifiable 10 — 1.00 over ten spans whose sha was never compared
    // is the claim the badge must not make.
    stubFetch([SPINE])
    render(wrap(<MorningRead />))
    const badge = await screen.findByTestId('read-record-badge')
    expect(badge.textContent).toMatch(/Quote fidelity: 6 of 16 spans fully checked/)
    expect(badge.textContent).not.toMatch(/1\.00 by construction/)
    expect(badge.textContent).not.toMatch(/not yet independently audited/)
  })

  it('prints the quote fidelity from the server-written counters when every span was fully checked', async () => {
    const full = JSON.parse(JSON.stringify(SPINE)) as {
      verification: { counters: Record<string, number> }
    }
    delete full.verification.counters.assembly_quote_sha_unverifiable
    full.verification.counters.assembly_quote_sha_verified = 16
    stubFetch([full as unknown as Row])
    render(wrap(<MorningRead />))
    const badge = await screen.findByTestId('read-record-badge')
    expect(badge.textContent).toMatch(/Quote fidelity 1\.00 by construction/)
  })

  it('a headline under 1.0 raises the gate banner and does not promise a withholding it cannot do', async () => {
    const failed = JSON.parse(JSON.stringify(SPINE)) as {
      verification: { branch_scores: { assembly: { score: number } } }
    }
    failed.verification.branch_scores.assembly.score = 0.85
    stubFetch([failed as unknown as Row])
    render(wrap(<MorningRead />))
    const banner = await screen.findByTestId('read-gate-failed')
    expect(banner.textContent).toMatch(/arms 0\.85/)
    expect(banner.textContent).toMatch(/none is withheld/)
    expect(screen.queryAllByTestId('read-block-refused')).toHaveLength(0)
  })
})
