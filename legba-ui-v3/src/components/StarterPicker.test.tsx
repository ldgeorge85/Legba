/**
 * UI-4 — StarterPicker + clone-into-editor integration.
 *
 * Asserts (acceptance: "a starter descriptor clones + opens in the editor"):
 *   - The picker lists the starters for its family.
 *   - Picking a starter calls onClone with a fresh body.
 *   - In the Targets registry panel, "+ starter" → pick → the inline
 *     DescriptorEditor opens pre-filled with the cloned descriptor YAML.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import { StarterPicker } from './StarterPicker'
import RegistryTargetsPanel from '@/panels/registry/Targets'
import type { PanelRegistration } from '@/types'

function reg(): PanelRegistration {
  return {
    id: 'r1',
    panel_id: 'registry_targets',
    descriptor_id: 'registry.targets',
    descriptor_version: 'v' + 'a'.repeat(63),
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Targets',
    mode: 'personal',
    layout_slot: 'registry.targets.main',
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

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('StarterPicker', () => {
  it('lists starters for the family and clones on pick', () => {
    const onClone = vi.fn()
    render(<StarterPicker family="target" onClone={onClone} />)
    const btn = screen.getByTestId('starter-target.geo_basic')
    expect(btn).toBeInTheDocument()
    fireEvent.click(btn)
    expect(onClone).toHaveBeenCalledTimes(1)
    const body = onClone.mock.calls[0][0] as Record<string, unknown>
    expect((body.identity as Record<string, unknown>).id).toBe('example_target')
  })
})

describe('Targets panel — starter clones into the inline editor', () => {
  it('opens the DescriptorEditor pre-filled from the picked starter', async () => {
    // Stub the registry list fetch so the panel renders without a network.
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => [] })
    vi.stubGlobal('fetch', fetchMock)

    render(wrap(<RegistryTargetsPanel registration={reg()} scope={{}} mode="personal" />))

    // open the starter picker
    fireEvent.click(screen.getByTestId('target-starter'))
    expect(screen.getByTestId('starter-picker')).toBeInTheDocument()

    // pick the basic geo target
    fireEvent.click(screen.getByTestId('starter-target.geo_basic'))

    // the inline YAML editor opens pre-filled with the starter body
    const textarea = document.querySelector('textarea') as HTMLTextAreaElement
    expect(textarea).toBeTruthy()
    expect(textarea.value).toContain('id: example_target')
    expect(textarea.value).toContain('schema_uri: legba/target/2.0.0')
  })

  it('renders the + build / + starter / + raw YAML actions', () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => [] })
    vi.stubGlobal('fetch', fetchMock)
    render(wrap(<RegistryTargetsPanel registration={reg()} scope={{}} mode="personal" />))
    expect(screen.getByTestId('target-build')).toBeInTheDocument()
    expect(screen.getByTestId('target-starter')).toBeInTheDocument()
    expect(screen.getByTestId('target-new-yaml')).toBeInTheDocument()
  })

  it('+ build opens the guided builder', () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => [] })
    vi.stubGlobal('fetch', fetchMock)
    render(wrap(<RegistryTargetsPanel registration={reg()} scope={{}} mode="personal" />))
    fireEvent.click(screen.getByTestId('target-build'))
    expect(screen.getByTestId('descriptor-builder')).toBeInTheDocument()
  })
})
