/**
 * Component test for the Settings panel (config-honesty stream).
 *
 * Covers:
 *  - first-run banner shows when config/status reports first_run
 *  - the three required model components render with status badges
 *  - saving routes the credential through the vault FIRST, then writes the
 *    component body with a vault REFERENCE (never the plaintext)
 *  - an existing-row save uses PUT; a fresh component uses POST
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import SettingsPanel from './Settings'
import type { PanelRegistration } from '@/types'

function reg(): PanelRegistration {
  return {
    id: 'd1',
    panel_id: 'system_settings',
    descriptor_id: '(singleton)',
    descriptor_version: 'v0',
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Settings',
    mode: 'personal',
    layout_slot: 'system.settings',
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

interface FetchCall {
  url: string
  method: string
  body: unknown
}

/**
 * Build a fetch stub. `components` maps a kind → the rows GET /stack?kind=…
 * returns. `statusOverride` lets a test pin config/status independently.
 * Captured calls land in `calls`.
 */
function stubFetch(opts: {
  components: Record<string, unknown[]>
  status: { first_run: boolean; all_configured: boolean; all_active: boolean; required: unknown[] }
  calls: FetchCall[]
}) {
  return vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET'
    const body = init?.body ? JSON.parse(init.body as string) : undefined
    opts.calls.push({ url, method, body })

    if (url.includes('/registry/config/status')) {
      return { ok: true, json: async () => opts.status }
    }
    if (url.includes('/registry/stack?kind=')) {
      const kind = decodeURIComponent(url.split('kind=')[1].split('&')[0])
      return { ok: true, json: async () => opts.components[kind] ?? [] }
    }
    if (url.includes('/registry/vault/secrets') && method === 'POST') {
      return { ok: true, json: async () => ({ secret_id: body.secret_id, version: 1 }) }
    }
    if (url.includes('/registry/stack/') && method === 'PUT') {
      return { ok: true, json: async () => ({ ...body, version: 'h'.repeat(64) }) }
    }
    if (url.endsWith('/registry/stack') && method === 'POST') {
      return { ok: true, json: async () => ({ ...body, version: 'h'.repeat(64) }) }
    }
    return { ok: true, json: async () => ({}) }
  })
}

const STATUS_FIRST_RUN = {
  first_run: true,
  all_configured: false,
  all_active: false,
  required: [
    { kind: 'llm_provider', configured: false, active: false, component_id: null, name: null, state: null },
    { kind: 'embedding', configured: false, active: false, component_id: null, name: null, state: null },
    { kind: 'nlp_service', configured: false, active: false, component_id: null, name: null, state: null },
  ],
}

const STATUS_ALL_ACTIVE = {
  first_run: false,
  all_configured: true,
  all_active: true,
  required: [
    { kind: 'llm_provider', configured: true, active: true, component_id: 'llm.primary.openai_compat', name: 'Primary LLM', state: 'active' },
    { kind: 'embedding', configured: true, active: true, component_id: 'embed.primary.openai_compat', name: 'Embed', state: 'active' },
    { kind: 'nlp_service', configured: true, active: true, component_id: 'nlp.local.legba_models', name: 'NLP', state: 'active' },
  ],
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.setItem('legba_token', 'test-token')
})

describe('SettingsPanel', () => {
  it('shows the first-run banner and all three required kinds when unconfigured', async () => {
    const calls: FetchCall[] = []
    vi.stubGlobal('fetch', stubFetch({ components: {}, status: STATUS_FIRST_RUN, calls }))
    render(wrap(<SettingsPanel registration={reg()} scope={{}} mode="personal" />))

    await waitFor(() => {
      expect(screen.getByTestId('settings-first-run')).toBeInTheDocument()
    })
    expect(screen.getByTestId('settings-kind-llm_provider')).toBeInTheDocument()
    expect(screen.getByTestId('settings-kind-embedding')).toBeInTheDocument()
    expect(screen.getByTestId('settings-kind-nlp_service')).toBeInTheDocument()
    // Unconfigured badge for the LLM provider.
    expect(screen.getByTestId('settings-badge-llm_provider')).toHaveTextContent('not configured')
  })

  it('hides the first-run banner when everything is active', async () => {
    const calls: FetchCall[] = []
    vi.stubGlobal(
      'fetch',
      stubFetch({
        components: {
          llm_provider: [{
            component_id: 'llm.primary.openai_compat', kind: 'llm_provider', state: 'active',
            name: 'Primary LLM', version: 'a'.repeat(64), schema_uri: 'legba/stack/llm_provider/1.0.0',
            is_head: true, owner: 'op', created_at: '2026-01-01T00:00:00Z',
            body: { config: { api_endpoint: { factory_kind: 'text', raw: 'https://llm.x' }, model_name: { factory_kind: 'text', raw: 'gpt-oss-120b' }, api_key: { factory_kind: 'secret', raw: 'llm.primary.api_key' } } },
          }],
          embedding: [{
            component_id: 'embed.primary.openai_compat', kind: 'embedding', state: 'active',
            name: 'Embed', version: 'b'.repeat(64), schema_uri: 'legba/stack/embedding/1.0.0',
            is_head: true, owner: 'op', created_at: '2026-01-01T00:00:00Z', body: { config: {} },
          }],
          nlp_service: [{
            component_id: 'nlp.local.legba_models', kind: 'nlp_service', state: 'active',
            name: 'NLP', version: 'c'.repeat(64), schema_uri: 'legba/stack/nlp_service/1.0.0',
            is_head: true, owner: 'op', created_at: '2026-01-01T00:00:00Z', body: { config: {} },
          }],
        },
        status: STATUS_ALL_ACTIVE,
        calls,
      }),
    )
    render(wrap(<SettingsPanel registration={reg()} scope={{}} mode="personal" />))

    await waitFor(() => {
      expect(screen.getByTestId('settings-badge-llm_provider')).toHaveTextContent('active')
    })
    expect(screen.queryByTestId('settings-first-run')).toBeNull()
    // The existing endpoint pre-fills the form.
    await waitFor(() => {
      expect(screen.getByTestId('settings-llm_provider-api_endpoint')).toHaveValue('https://llm.x')
    })
  })

  it('registers a fresh LLM component: secret to vault FIRST, body carries a ref not the plaintext', async () => {
    const calls: FetchCall[] = []
    vi.stubGlobal('fetch', stubFetch({ components: {}, status: STATUS_FIRST_RUN, calls }))
    render(wrap(<SettingsPanel registration={reg()} scope={{}} mode="personal" />))

    await waitFor(() => screen.getByTestId('settings-kind-llm_provider'))

    fireEvent.change(screen.getByTestId('settings-llm_provider-api_endpoint'), {
      target: { value: 'https://llm.example.internal' },
    })
    fireEvent.change(screen.getByTestId('settings-llm_provider-model_name'), {
      target: { value: 'gpt-oss-120b' },
    })
    fireEvent.change(screen.getByTestId('settings-llm_provider-api_key'), {
      target: { value: 'sk-super-secret-never-leak' },
    })
    fireEvent.click(screen.getByTestId('settings-save-llm_provider'))

    await waitFor(() => {
      expect(screen.getByTestId('settings-msg-llm_provider')).toHaveTextContent('Saved.')
    })

    const vaultCall = calls.find(
      (c) => c.url.includes('/registry/vault/secrets') && c.method === 'POST',
    )
    const stackCall = calls.find(
      (c) => c.url.endsWith('/registry/stack') && c.method === 'POST',
    )
    expect(vaultCall).toBeTruthy()
    expect(stackCall).toBeTruthy()
    // Vault write happened before the component write.
    expect(calls.indexOf(vaultCall!)).toBeLessThan(calls.indexOf(stackCall!))
    // The vault got the plaintext under the seed id.
    expect((vaultCall!.body as { secret_id: string }).secret_id).toBe('llm.primary.api_key')
    expect((vaultCall!.body as { plaintext: string }).plaintext).toBe('sk-super-secret-never-leak')
    // The component body carries a SECRET REF, never the plaintext.
    const stackBody = stackCall!.body as { config: Record<string, { factory_kind: string; raw: string }> }
    expect(stackBody.config.api_key).toEqual({ factory_kind: 'secret', raw: 'llm.primary.api_key' })
    expect(JSON.stringify(stackBody)).not.toContain('sk-super-secret-never-leak')
  })

  it('updates an existing component with PUT and keeps the secret ref when the field is left blank', async () => {
    const calls: FetchCall[] = []
    vi.stubGlobal(
      'fetch',
      stubFetch({
        components: {
          llm_provider: [{
            component_id: 'llm.primary.openai_compat', kind: 'llm_provider', state: 'active',
            name: 'Primary LLM', version: 'a'.repeat(64), schema_uri: 'legba/stack/llm_provider/1.0.0',
            is_head: true, owner: 'op', created_at: '2026-01-01T00:00:00Z',
            body: { config: { api_endpoint: { factory_kind: 'text', raw: 'https://old' }, model_name: { factory_kind: 'text', raw: 'old-model' }, api_key: { factory_kind: 'secret', raw: 'llm.primary.api_key' } } },
          }],
        },
        status: STATUS_ALL_ACTIVE,
        calls,
      }),
    )
    render(wrap(<SettingsPanel registration={reg()} scope={{}} mode="personal" />))

    await waitFor(() => {
      expect(screen.getByTestId('settings-llm_provider-api_endpoint')).toHaveValue('https://old')
    })
    fireEvent.change(screen.getByTestId('settings-llm_provider-api_endpoint'), {
      target: { value: 'https://new.endpoint' },
    })
    // Leave the api_key field blank → no vault write, ref preserved.
    fireEvent.click(screen.getByTestId('settings-save-llm_provider'))

    await waitFor(() => {
      expect(screen.getByTestId('settings-msg-llm_provider')).toHaveTextContent('Saved.')
    })
    const putCall = calls.find(
      (c) => c.url.includes('/registry/stack/') && c.method === 'PUT',
    )
    expect(putCall).toBeTruthy()
    // No vault POST since the credential field was left blank.
    expect(calls.find((c) => c.url.includes('/vault/secrets') && c.method === 'POST')).toBeUndefined()
    const putBody = putCall!.body as { config: Record<string, { raw: string }> }
    expect(putBody.config.api_endpoint.raw).toBe('https://new.endpoint')
    // The existing secret ref survives the blank-field update.
    expect(putBody.config.api_key).toEqual({ factory_kind: 'secret', raw: 'llm.primary.api_key' })
  })
})
