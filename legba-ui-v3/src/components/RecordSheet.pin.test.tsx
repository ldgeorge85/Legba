/**
 * The Record sheet claims Consult's "asking about" point only for a record it
 * could read: a record that failed to resolve (not found, or unreadable) must
 * not become what the next question is asked about.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { LINEAGE, FINDING_ID, RECEIPTS, INTEGRITY } from '@/test/recordFixture'

const apiGet = vi.fn()
vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  apiGet: (path: string) => apiGet(path),
}))

import { ApiError } from '@/lib/api'
import { RecordSheet } from '@/components/RecordSheet'
import { resolveAmbientPin, useAmbientPins } from '@/lib/consultContext'
import { useSelection } from '@/state/selection'
import { useRecordSheet } from '@/lib/recordSheet'

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <RecordSheet open mode="personal" onClose={() => {}} />
    </QueryClientProvider>,
  )
}

const point = () => resolveAmbientPin(useAmbientPins.getState().claims, null)

beforeEach(() => {
  apiGet.mockReset()
  useAmbientPins.setState({ claims: [] })
  useRecordSheet.setState({ ref: null, cite: null, open: false })
  useSelection.setState({ selection: { kind: 'finding', id: FINDING_ID, label: 'A finding' } as never })
})

afterEach(() => {
  useSelection.setState({ selection: null })
})

describe('the Record sheet and the point Consult asks about', () => {
  it('a record that cannot be found is not made the point', async () => {
    apiGet.mockImplementation(async (path: string) => {
      if (path.startsWith('/lineage/')) throw new ApiError(404, { detail: 'not found' })
      throw new Error(`unexpected ${path}`)
    })
    mount()
    await screen.findByText(/no such record was found/)
    expect(point()).toBeNull()
  })

  it('a record that was read is the point', async () => {
    apiGet.mockImplementation(async (path: string) => {
      if (path.startsWith('/lineage/')) return LINEAGE
      if (path.startsWith('/v3/record/receipts')) return RECEIPTS
      if (path.startsWith('/v3/record/integrity')) return INTEGRITY
      throw new Error(`unexpected ${path}`)
    })
    mount()
    await screen.findByTestId('record-statement')
    await waitFor(() => expect(point()?.pin.id).toBe(FINDING_ID))
  })
})
