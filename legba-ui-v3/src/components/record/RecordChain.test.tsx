/**
 * RecordChain (U-10) — the sheet reads one statement top to bottom from
 * invented rows: statement → row → evidence → receipt → later versions →
 * integrity. Pinned: the bounded reads it makes (and the text only on
 * demand), the received text shown as TEXT (never rendered as HTML), each gap
 * in words, and no "verified" for what was not checked.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { FINDING_ID, INTEGRITY, LINEAGE, RECEIPTS, SIG_LINKED, SIG_OLD, TEXT_102 } from '@/test/recordFixture'

const apiGet = vi.fn()
vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  apiGet: (path: string) => apiGet(path),
}))

import RecordChain from './RecordChain'

function routes(over: Record<string, unknown> = {}) {
  apiGet.mockImplementation(async (path: string) => {
    for (const [prefix, body] of Object.entries(over)) if (path.startsWith(prefix)) return body
    if (path.startsWith('/lineage/finding/')) return LINEAGE
    if (path.startsWith('/v3/record/receipts')) return RECEIPTS
    if (path.startsWith('/v3/record/integrity')) return INTEGRITY
    if (path.startsWith('/v3/record/version/102/text')) return TEXT_102
    throw new Error(`unexpected ${path}`)
  })
}

function mount(cite: number | null = null) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <RecordChain record={{ kind: 'finding', id: FINDING_ID }} cite={cite} />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  apiGet.mockReset()
  routes()
})

describe('the chain', () => {
  it('reads a whole statement in three bounded reads, the text only on demand', async () => {
    mount()
    await waitFor(() => expect(screen.getAllByTestId('record-integrity-line')).toHaveLength(3))
    await waitFor(() => expect(screen.getAllByTestId('record-receipt')).toHaveLength(2))
    const paths = apiGet.mock.calls.map((c) => c[0] as string)
    expect(paths).toEqual([
      `/lineage/finding/${FINDING_ID}?direction=upstream&depth=1`,
      `/v3/record/receipts?kind=signal&ids=${SIG_LINKED},${SIG_OLD}`,
      `/v3/record/integrity?run_id=${LINEAGE.root.receipt!.run_id}`,
    ])
    expect(screen.getByTestId('record-statement')).toHaveTextContent('The cranes at the north quay stood idle for a third day.')
    expect(screen.getByTestId('record-row-line')).toHaveTextContent('Written by Harbour watch on 13 Nov 2026, 08:10 UTC.')
    const rows = screen.getAllByTestId('record-evidence')
    expect(rows).toHaveLength(2)
    expect(within(rows[0]).getByText(/Harbour Gazette · reporting/)).toBeInTheDocument()
    expect(within(rows[0]).getAllByTestId('record-receipt').map((r) => r.getAttribute('data-version'))).toEqual(['101', '102'])
    expect(within(rows[0]).getByTestId('record-later-summary')).toHaveTextContent(/Changed after it was read: a substantive edit/)
    expect(within(rows[0]).getAllByTestId('record-receipt-held')[1]).toHaveTextContent(/cold storage/)
    expect(within(rows[1]).getByTestId('record-no-receipt')).toHaveTextContent(/before fetch receipts were kept/)
  })

  it('a cited sentence leads, with exactly its own evidence', async () => {
    mount(1)
    await waitFor(() => expect(screen.getAllByTestId('record-evidence')).toHaveLength(1))
    expect(screen.getByTestId('record-statement')).toHaveAttribute('data-basis', 'cited_sentence')
    expect(screen.getByTestId('record-evidence')).toHaveAttribute('data-id', SIG_LINKED)
    expect(apiGet.mock.calls.map((c) => c[0])).toContain(`/v3/record/receipts?kind=signal&ids=${SIG_LINKED}`)
  })

  it('"Show what was received" reads the stored text once, as text, never as HTML', async () => {
    const { container } = mount(1)
    await waitFor(() => expect(screen.getAllByTestId('record-show-received')).toHaveLength(1))
    // the feed response kept no text: said in words, no read made for it
    expect(screen.getByTestId('record-no-text')).toHaveTextContent(/kept as bytes only/)
    fireEvent.click(screen.getByTestId('record-show-received'))
    await waitFor(() => expect(screen.getByTestId('record-text')).toBeInTheDocument())
    expect(apiGet.mock.calls.filter((c) => String(c[0]).includes('/text')).map((c) => c[0])).toEqual([
      '/v3/record/version/102/text?max_chars=20000',
    ])
    expect(screen.getByTestId('record-text-extent')).toHaveTextContent(/Showing the first 60 of 412 characters — truncated/)
    expect(screen.getByTestId('record-text-hash')).toHaveTextContent(/Hash verified/)
    expect(container.querySelector('pre')!.textContent).toContain('<script>alert(1)</script>')
    expect(container.querySelector('script')).toBeNull()
  })

  it('a withheld text says why, and shows nothing', async () => {
    routes({ '/v3/record/version/102/text': { ...TEXT_102, shown: false, withheld_reason: 'reader_display_policy', full_text: null } })
    mount(1)
    await waitFor(() => expect(screen.getAllByTestId('record-show-received')).toHaveLength(1))
    fireEvent.click(screen.getByTestId('record-show-received'))
    await waitFor(() => expect(screen.getByTestId('record-text-withheld')).toHaveTextContent(/admins and owners only/))
    expect(screen.queryByTestId('record-text')).toBeNull()
  })

  it('states integrity in words; nothing is "verified" that was not checked', async () => {
    mount()
    await waitFor(() => expect(screen.getAllByTestId('record-integrity-line')).toHaveLength(3))
    const states = screen.getAllByTestId('record-integrity-line').map((l) => l.getAttribute('data-state'))
    expect(states).toEqual(['verified', 'covered', 'covered'])
    expect(screen.getAllByTestId('record-integrity-line')[1]).toHaveTextContent(/covered, not checked here/)
  })

  it('a marker with no citation is a gap at the statement and at the evidence', async () => {
    mount(3)
    await waitFor(() => expect(screen.getByTestId('record-statement-gap')).toBeInTheDocument())
    expect(screen.getByTestId('record-statement-gap')).toHaveTextContent(/maps to no row/)
    expect(screen.getByTestId('record-evidence-gap')).toHaveTextContent(/maps to no row/)
    expect(apiGet.mock.calls.map((c) => String(c[0])).some((p) => p.startsWith('/v3/record/receipts'))).toBe(false)
  })

  it('a record the route cannot read says so in words', async () => {
    const { ApiError } = await import('@/lib/api')
    apiGet.mockImplementation(async () => {
      throw new ApiError(404, { detail: 'no row' })
    })
    mount()
    await waitFor(() => expect(screen.getAllByTestId('record-gap')[0]).toHaveTextContent(/no such record was found/))
  })

  it('B-1: the evidence label is byte_path\'s, and a feed response opens the ITEM as received on demand', async () => {
    const withEvidence = {
      ...RECEIPTS,
      records: [
        {
          ...RECEIPTS.records[0],
          evidence: { label: 'received_bytes', label_text: 'received bytes', version_id: 101, link_role: 'response', has_text: true, hash_only: [] },
        },
        { ...RECEIPTS.records[1], evidence: { label: 'no_bytes_kept', label_text: 'no bytes kept', version_id: null, link_role: null, has_text: false, hash_only: [] } },
      ],
    }
    const item = {
      available: true, signal_id: SIG_LINKED, found: true, evidence: withEvidence.records[0].evidence, version_id: 101,
      basis: 'feed_item', exact: true, shown: true, withheld_reason: null, chars_total: 31, chars_shown: 31, truncated: false,
      text_sha256: 'f'.repeat(64), item_sha256: 'e'.repeat(64), version_sha256: 'd'.repeat(64),
      full_text: '<b>Cranes idle</b> at the quay',
    }
    routes({ '/v3/record/receipts': withEvidence, [`/v3/record/signal/${SIG_LINKED}/item`]: item })
    const { container } = mount()
    await waitFor(() => expect(screen.getAllByTestId('record-evidence-label')).toHaveLength(2))
    const [linked, old] = screen.getAllByTestId('record-evidence-label')
    expect(linked).toHaveAttribute('data-label', 'received_bytes')
    expect(linked).toHaveTextContent(/^Received bytes:/)
    expect(old).toHaveTextContent(/^No bytes kept:/)
    // the response (101) offers the item; the page (102) keeps its own text button
    expect(screen.getAllByTestId('record-show-item')).toHaveLength(1)
    expect(apiGet.mock.calls.map((c) => String(c[0])).some((p) => p.includes('/item'))).toBe(false)
    fireEvent.click(screen.getByTestId('record-show-item'))
    await waitFor(() => expect(screen.getByTestId('record-item')).toBeInTheDocument())
    expect(apiGet.mock.calls.map((c) => String(c[0]))).toContain(`/v3/record/signal/${SIG_LINKED}/item?max_chars=20000`)
    expect(screen.getByTestId('record-item')).toHaveTextContent('<b>Cranes idle</b> at the quay')
    expect(container.querySelector('[data-testid="record-item"] b')).toBeNull()   // text, never HTML
  })
})
