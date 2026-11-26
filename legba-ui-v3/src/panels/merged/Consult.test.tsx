/**
 * Component test for the merged Consult (`system.consult`) — Deep is a mode
 * switch inside the one composer, not a second tab or form: the same box asks
 * either a quick question (answered in the conversation) or a deep one (a
 * long task that is polled and writes a finding).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import type { PanelRegistration } from '@/types'
import { useConsultSessions } from '@/state/consultSession'
import ConsultMerged from './Consult'

function reg(): PanelRegistration {
  return {
    id: 'p', panel_id: 'system_consult', descriptor_id: '(singleton)', descriptor_version: '0'.repeat(64),
    descriptor_family: 'target', analyst_id: null, title: 'Consult', mode: 'personal',
    layout_slot: 'x', data_query: {}, binding: {}, retired: false,
    created_at: '2026-06-03T00:00:00Z', retired_at: null,
  }
}

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  useConsultSessions.setState({ panels: {} })
  fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if (String(url).endsWith('/deep_consult') && init?.method === 'POST') {
      return { ok: true, status: 202, json: async () => ({ task_id: 'task-1' }) }
    }
    if (String(url).includes('/deep_consult/task-1')) {
      return { ok: true, status: 200, json: async () => ({ task_id: 'task-1', status: 'completed', answer: 'The deep answer.', finding_id: 'f-9', cited_refs: [], fact_ids: [], hypothesis_ids: [] }) }
    }
    return { ok: true, status: 200, json: async () => [] }
  })
  vi.stubGlobal('fetch', fetchMock)
})

const deepPosts = () =>
  fetchMock.mock.calls.filter(([u, i]) => String(u).endsWith('/deep_consult') && (i as RequestInit)?.method === 'POST')

describe('ConsultMerged', () => {
  it('opens as a quick-answer composer with no Chat/Deep tab strip', async () => {
    render(<ConsultMerged registration={reg()} scope={{}} mode="personal" />)
    expect(await screen.findByTestId('consult-question')).toBeInTheDocument()
    expect(screen.getByTestId('consult-deep-toggle')).toHaveAttribute('aria-checked', 'false')
    expect(screen.queryByTestId('consult-depth-chat')).toBeNull()
    expect(screen.queryByTestId('consult-depth-deep')).toBeNull()
  })

  it('the retired deep address opens with Deep already on', async () => {
    render(<ConsultMerged registration={reg()} scope={{}} mode="personal" initialTab="deep" />)
    expect(await screen.findByTestId('consult-deep-toggle')).toHaveAttribute('aria-checked', 'true')
  })

  it('with Deep on, Ask starts the long task from the same box and shows its answer in the transcript area', async () => {
    render(<ConsultMerged registration={reg()} scope={{}} mode="personal" />)
    fireEvent.click(await screen.findByTestId('consult-deep-toggle'))
    fireEvent.change(screen.getByTestId('consult-question'), { target: { value: 'why did it move?' } })
    fireEvent.click(screen.getByTestId('consult-submit'))
    await waitFor(() => expect(deepPosts()).toHaveLength(1))
    const body = JSON.parse((deepPosts()[0][1] as RequestInit).body as string)
    expect(body).toMatchObject({ emit_facts: true, emit_hypotheses: true })
    expect(body.question).toContain('why did it move?')
    expect(await screen.findByTestId('deep-consult-result')).toBeInTheDocument()
    // the same box is cleared and no chat turn was posted
    expect((screen.getByTestId('consult-question') as HTMLTextAreaElement).value).toBe('')
    expect(fetchMock.mock.calls.some(([u]) => String(u).endsWith('/consult'))).toBe(false)
  })

  it('switching Deep off returns Ask to a quick question', async () => {
    render(<ConsultMerged registration={reg()} scope={{}} mode="personal" initialTab="deep" />)
    fireEvent.click(await screen.findByTestId('consult-deep-toggle'))
    expect(screen.getByTestId('consult-deep-toggle')).toHaveAttribute('aria-checked', 'false')
    expect(screen.getByTestId('consult-question').getAttribute('placeholder')).toMatch(/^Ask a question/)
  })
})
