/**
 * Component test for the `registry.action_packs` panel.
 *
 * Mocks the registry at the HTTP boundary (apiGet / PUT → fetch), routing by
 * URL: GET /registry/action_packs (pack list), GET /registry/descriptors/
 * {analyst,target}/{id} (grant-bearing scopes), PUT (grant/revoke write).
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import RegistryActionPacksPanel from './ActionPacks'
import type { PanelRegistration } from '@/types'

function reg(): PanelRegistration {
  return {
    id: 'r1',
    panel_id: 'registry_action_packs',
    descriptor_id: 'registry.action_packs',
    descriptor_version: 'v'.repeat(64),
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Action-Pack Grants',
    mode: 'personal',
    layout_slot: 'main',
    data_query: {},
    binding: {},
    retired: false,
    created_at: '2026-06-01T00:00:00Z',
    retired_at: null,
  }
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

const PACKS = [
  {
    descriptor_id: 'discovery',
    version: 'a'.repeat(64),
    schema_uri: 'legba/action_pack/1.0.0',
    is_head: true,
    state: 'active',
    owner: 'op',
    name: 'Source Discovery',
    abstraction_level: 'L1',
    inherits: [],
    created_at: '2026-06-01T00:00:00Z',
    retire_after: null,
    tool_names: ['discover_sources'],
    channel_names: [],
    applies_to_tags: ['news'],
    has_governor: true,
    body: {
      identity: { id: 'discovery', version: 'a'.repeat(64) },
      tools: [{ name: 'discover_sources' }],
      applies_to_tags: ['news'],
      governor: { max_sources_per_window: 25, crawl_max_depth: 2 },
    },
  },
  {
    descriptor_id: 'media_processing',
    version: 'b'.repeat(64),
    schema_uri: 'legba/action_pack/1.0.0',
    is_head: true,
    state: 'active',
    owner: 'op',
    name: 'Media Processing',
    abstraction_level: 'L1',
    inherits: [],
    created_at: '2026-06-01T00:00:00Z',
    retire_after: null,
    tool_names: ['process_media'],
    channel_names: [],
    applies_to_tags: [],
    has_governor: false,
    body: {
      identity: { id: 'media_processing', version: 'b'.repeat(64) },
      tools: [{ name: 'process_media' }],
    },
  },
]

const ANALYST_ROW = {
  descriptor_id: 'analyst.osint',
  version: 'c'.repeat(64),
  state: 'active',
  body: {
    identity: { id: 'analyst.osint', version: 'c'.repeat(64) },
    action_packs: [{ pack_id: 'discovery' }],
  },
}

const TARGET_ROW = {
  descriptor_id: 'target.geo.brazil',
  version: 'd'.repeat(64),
  state: 'active',
  body: {
    identity: { id: 'target.geo.brazil', version: 'd'.repeat(64) },
    scope: { tags: ['news'] },
    allowed_action_packs: [{ pack_id: 'discovery' }],
  },
}

let putBodies: Array<{ url: string; body: unknown }> = []

function mockFetch() {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const u = String(url)
    if (init?.method === 'PUT') {
      putBodies.push({ url: u, body: JSON.parse(String(init.body)) })
      return { ok: true, json: async () => ({ version: 'e'.repeat(64) }) }
    }
    if (u.includes('/registry/action_packs')) {
      return { ok: true, json: async () => PACKS }
    }
    if (u.includes('/registry/descriptors/analyst/')) {
      return { ok: true, json: async () => ANALYST_ROW }
    }
    if (u.includes('/registry/descriptors/target/')) {
      return { ok: true, json: async () => TARGET_ROW }
    }
    // ScopePicker descriptor lists
    if (u.includes('/registry/descriptors?family=analyst')) {
      return { ok: true, json: async () => [{ descriptor_id: 'analyst.osint', state: 'active' }] }
    }
    if (u.includes('/registry/descriptors?family=target')) {
      return {
        ok: true,
        json: async () => [{ descriptor_id: 'target.geo.brazil', state: 'active' }],
      }
    }
    return { ok: true, json: async () => [] }
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  putBodies = []
})

describe('RegistryActionPacksPanel', () => {
  it('lists action packs from GET /registry/action_packs', async () => {
    mockFetch()
    render(wrap(<RegistryActionPacksPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => {
      expect(screen.getByTestId('action-pack-row-discovery')).toBeInTheDocument()
    })
    expect(screen.getByTestId('action-pack-row-media_processing')).toBeInTheDocument()
  })

  it('filters packs by search query', async () => {
    mockFetch()
    render(wrap(<RegistryActionPacksPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => screen.getByTestId('action-pack-row-discovery'))
    fireEvent.change(screen.getByTestId('action-packs-search'), {
      target: { value: 'media' },
    })
    expect(screen.queryByTestId('action-pack-row-discovery')).not.toBeInTheDocument()
    expect(screen.getByTestId('action-pack-row-media_processing')).toBeInTheDocument()
  })

  it('shows governor caps when a governed pack is expanded', async () => {
    mockFetch()
    render(wrap(<RegistryActionPacksPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => screen.getByTestId('action-pack-row-discovery'))
    fireEvent.click(screen.getByTestId('action-pack-row-discovery').querySelector('button')!)
    const gov = await screen.findByTestId('action-pack-governor-discovery')
    expect(gov.textContent).toContain('sources/win: 25')
    expect(gov.textContent).toContain('crawl depth: 2')
  })

  it('computes the effective three-way intersection once analyst + target are bound', async () => {
    mockFetch()
    render(wrap(<RegistryActionPacksPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => screen.getByTestId('action-pack-row-discovery'))

    fireEvent.change(screen.getByTestId('action-pack-analyst-picker'), {
      target: { value: 'analyst.osint' },
    })
    fireEvent.change(screen.getByTestId('action-pack-target-picker'), {
      target: { value: 'target.geo.brazil' },
    })

    // `discovery` is granted by the analyst, allowed by the target, and its
    // applies_to_tags=['news'] overlaps the target scope tags=['news'] → EFFECTIVE.
    await waitFor(() => {
      expect(screen.getByTestId('action-pack-effective-discovery').textContent).toContain(
        'EFFECTIVE',
      )
    })
    // `media_processing` is neither granted nor allowed → not effective.
    expect(
      screen.getByTestId('action-pack-effective-media_processing').textContent,
    ).toContain('not effective')
  })

  it('grants a pack to a target by PUT-ing an updated allowed_action_packs', async () => {
    mockFetch()
    render(wrap(<RegistryActionPacksPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => screen.getByTestId('action-pack-row-media_processing'))

    fireEvent.change(screen.getByTestId('action-pack-target-picker'), {
      target: { value: 'target.geo.brazil' },
    })
    // wait for the target body to load (target tags chip appears)
    await screen.findByTestId('action-pack-target-tags')

    fireEvent.click(
      screen.getByTestId('action-pack-row-media_processing').querySelector('button')!,
    )
    fireEvent.click(screen.getByTestId('action-pack-allow-target-media_processing'))

    await waitFor(() => {
      expect(putBodies.length).toBe(1)
    })
    const put = putBodies[0]
    expect(put.url).toContain('/registry/descriptors/target/target.geo.brazil')
    const body = put.body as { allowed_action_packs: Array<{ pack_id: string }> }
    const ids = body.allowed_action_packs.map((r) => r.pack_id)
    expect(ids).toContain('discovery') // pre-existing grant preserved
    expect(ids).toContain('media_processing') // newly granted
  })

  it('revokes a pack from a target by removing it from allowed_action_packs', async () => {
    mockFetch()
    render(wrap(<RegistryActionPacksPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => screen.getByTestId('action-pack-row-discovery'))

    fireEvent.change(screen.getByTestId('action-pack-target-picker'), {
      target: { value: 'target.geo.brazil' },
    })
    await screen.findByTestId('action-pack-target-tags')

    fireEvent.click(screen.getByTestId('action-pack-row-discovery').querySelector('button')!)
    // discovery is already allowed → button revokes it
    fireEvent.click(screen.getByTestId('action-pack-allow-target-discovery'))

    await waitFor(() => {
      expect(putBodies.length).toBe(1)
    })
    const body = putBodies[0].body as { allowed_action_packs: Array<{ pack_id: string }> }
    expect(body.allowed_action_packs.map((r) => r.pack_id)).not.toContain('discovery')
  })
})
