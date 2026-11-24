/**
 * ScopePicker on the Select primitive (UI-B1/tokens): the registry-backed
 * options, the explicit none-row mapped back to "", an id not (yet) in the
 * list still shown as itself, and the portal host.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useState, type ReactElement } from 'react'
import { ScopePicker } from './ScopePicker'
import { PortalHost } from '@/ui/portal'
import { pickScopeOption } from '@/test/pickScope'

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <PortalHost>{ui}</PortalHost>
    </QueryClientProvider>
  )
}

const ROWS = [
  { descriptor_id: 'source.rss.alpha', name: 'Alpha wire', state: 'active' },
  { descriptor_id: 'source.rss.beta', name: null, state: 'active' },
  { descriptor_id: 'source.rss.retired', name: 'Gone', state: 'retired' },
]

function Harness({ initial = '', allowEmpty = true }: { initial?: string; allowEmpty?: boolean }) {
  const [v, setV] = useState(initial)
  return (
    <>
      <ScopePicker family="source" value={v} onChange={setV} allowEmpty={allowEmpty} testId="pick" />
      <span data-testid="value">{v}</span>
    </>
  )
}

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({ ok: true, status: 200, json: async () => ROWS })),
  )
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('ScopePicker', () => {
  it('shows its placeholder with no value', () => {
    render(wrap(<Harness />))
    expect(screen.getByTestId('pick')).toHaveTextContent('select source…')
    expect(screen.getByTestId('pick')).toHaveAttribute('data-value', '')
  })

  it('lists the ACTIVE registry descriptors, labelled "<id> — <name>"', async () => {
    render(wrap(<Harness />))
    fireEvent.keyDown(screen.getByTestId('pick'), { key: 'Enter' })
    expect(await screen.findByTestId('pick-option-source.rss.alpha')).toHaveTextContent(
      'source.rss.alpha — Alpha wire',
    )
    expect(screen.getByTestId('pick-option-source.rss.beta')).toHaveTextContent(/^source\.rss\.beta$/)
    expect(screen.queryByTestId('pick-option-source.rss.retired')).toBeNull()
    expect(screen.getByTestId('ui-portal-host').contains(screen.getByRole('listbox'))).toBe(true)
  })

  it('picks a descriptor, and the none-row maps back to an empty value', async () => {
    render(wrap(<Harness />))
    await pickScopeOption('pick', 'source.rss.alpha')
    expect(screen.getByTestId('value')).toHaveTextContent('source.rss.alpha')
    expect(screen.getByTestId('pick')).toHaveTextContent('source.rss.alpha — Alpha wire')
    fireEvent.keyDown(screen.getByTestId('pick'), { key: 'Enter' })
    fireEvent.click(await screen.findByTestId('pick-option-none'))
    await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent(/^$/))
    expect(screen.getByTestId('pick')).toHaveTextContent('select source…')
  })

  it('allowEmpty={false} offers no none-row', async () => {
    render(wrap(<Harness allowEmpty={false} />))
    fireEvent.keyDown(screen.getByTestId('pick'), { key: 'Enter' })
    await screen.findByTestId('pick-option-source.rss.alpha')
    expect(screen.queryByTestId('pick-option-none')).toBeNull()
  })

  it('an id not in the loaded list still shows as itself, never as a blank', () => {
    render(wrap(<Harness initial="source.rss.unknown" />))
    expect(screen.getByTestId('pick')).toHaveTextContent('source.rss.unknown')
    expect(screen.getByTestId('pick')).toHaveAttribute('data-value', 'source.rss.unknown')
  })
})
