/**
 * An alert's page on the phone: Acknowledge and Snooze for one who may act on
 * an OPEN episode; a closed one takes no act and says so (at 360 px a
 * closed episode's page once showed no controls and no reason).
 */
import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { AttentionOut } from './AlertActions'

const attention = (state: 'open' | 'closed'): AttentionOut => ({
  episode_id: 'ex-alert-1',
  state,
  attention: { ack_active: false, snooze_active: false, resurfaced: false, hidden_from_needs_you: false },
  history: [],
  shared_note: 'Shared.',
  paging_note: 'Paging is unchanged.',
  snooze_options: ['1h', '4h', '24h', 'until_closed'],
})

let current: AttentionOut = attention('open')
let role = 'operator'
vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  apiGet: vi.fn(async () => current),
}))
vi.mock('@/auth/SignInGate', () => ({
  useIdentity: () => ({ data: { state: 'signed_in', identity: { kind: role === 'reader' ? 'user' : 'shared', role } } }),
}))

import { AlertActions } from './AlertActions'

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <AlertActions id="ex-alert-1" />
    </QueryClientProvider>,
  )
}

describe('AlertActions', () => {
  it('an open episode: Acknowledge and the snooze choices', async () => {
    current = attention('open')
    mount()
    expect(await screen.findByTestId('phone-alert-ack')).toBeTruthy()
    expect(screen.getAllByTestId(/^phone-alert-snooze-/)).toHaveLength(4)
    expect(screen.queryByTestId('phone-alert-closed')).toBeNull()
  })

  it('a closed episode: no control, and the page says why', async () => {
    current = attention('closed')
    mount()
    expect((await screen.findByTestId('phone-alert-closed')).textContent).toMatch(/closed.*nothing to acknowledge or snooze/)
    expect(screen.queryByTestId('phone-alert-ack')).toBeNull()
    expect(screen.queryAllByTestId(/^phone-alert-snooze-/)).toHaveLength(0)
  })
  it('a reader: an open episode says who can act; a closed one says it has closed', async () => {
    role = 'reader'
    try {
      current = attention('open')
      const first = mount()
      expect(await screen.findByTestId('phone-alert-reader')).toBeTruthy()
      expect(screen.queryByTestId('phone-alert-ack')).toBeNull()
      first.unmount()
      current = attention('closed')
      mount()
      expect(await screen.findByTestId('phone-alert-closed')).toBeTruthy()
      expect(screen.queryByTestId('phone-alert-reader')).toBeNull()
      expect(screen.queryByTestId('phone-alert-ack')).toBeNull()
    } finally {
      role = 'operator'
    }
  })
})
