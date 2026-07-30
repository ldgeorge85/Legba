/**
 * Component test for the U-3 merged Consult (`system.consult`) — proves the
 * Chat/Deep depth toggle actually swaps between the two ORIGINAL, unmodified
 * implementations (the chat Consult panel / the async Deep Consult panel)
 * rather than silently dropping one.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import type { PanelRegistration } from '@/types'
import ConsultMerged from './Consult'

function reg(): PanelRegistration {
  return {
    id: 'p', panel_id: 'system_consult', descriptor_id: '(singleton)', descriptor_version: '0'.repeat(64),
    descriptor_family: 'target', analyst_id: null, title: 'Consult', mode: 'personal',
    layout_slot: 'x', data_query: {}, binding: {}, retired: false,
    created_at: '2026-06-03T00:00:00Z', retired_at: null,
  }
}

// Neither Consult nor Deep Consult needs a QueryClientProvider (they use
// react-query's useQuery nowhere — Consult holds its own transcript state;
// Deep Consult polls via a plain setInterval) — only `fetch` needs stubbing
// for Deep Consult's on-mount task-history load (`/consult/sessions`, a bare
// array per `listConsultSessions`'s return type).
function stubFetch() {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => [] }))
}

beforeEach(() => {
  vi.restoreAllMocks()
  stubFetch()
})

describe('ConsultMerged', () => {
  it('defaults to the Chat tab (the original chat Consult panel)', async () => {
    render(<ConsultMerged registration={reg()} scope={{}} mode="personal" />)
    expect(await screen.findByTestId('consult-question')).toBeInTheDocument()
    expect(screen.getByTestId('consult-depth-chat')).toHaveAttribute('aria-selected', 'true')
  })

  it('switching to Deep mounts the original Deep Consult panel', async () => {
    render(<ConsultMerged registration={reg()} scope={{}} mode="personal" />)
    await screen.findByTestId('consult-question')
    fireEvent.click(screen.getByTestId('consult-depth-deep'))
    expect(await screen.findByTestId('deep-consult-history')).toBeInTheDocument()
    expect(screen.queryByTestId('consult-question')).not.toBeInTheDocument()
    expect(screen.getByTestId('consult-depth-deep')).toHaveAttribute('aria-selected', 'true')
  })

  it('switching back to Chat remounts the chat panel', async () => {
    render(<ConsultMerged registration={reg()} scope={{}} mode="personal" />)
    await screen.findByTestId('consult-question')
    fireEvent.click(screen.getByTestId('consult-depth-deep'))
    await screen.findByTestId('deep-consult-history')
    fireEvent.click(screen.getByTestId('consult-depth-chat'))
    expect(await screen.findByTestId('consult-question')).toBeInTheDocument()
  })
})
