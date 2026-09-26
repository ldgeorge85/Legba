/**
 * Component test for the mobile ConsultSheet's F1 model picker.
 *
 * The sheet DOES have a submit control (`consult-send`), so it gets the same
 * compact model select the workstation Consult panel has — same persisted
 * `legba_consult_model` key, same options (Opus 5 / Fable 5.1 / Core
 * (self-hosted)), same default.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import ConsultSheet from './ConsultSheet'
import { useConsultSessions } from '@/state/consultSession'

function answerOnce(answer: string, extra: Record<string, unknown> = {}) {
  return vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({
      answer,
      finding_id: null,
      derived_from: [],
      tool_calls: [],
      cited_refs: [],
      ...extra,
    }),
  })
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  useConsultSessions.setState({ panels: {} })
})

describe('ConsultSheet (F1 model picker)', () => {
  it('defaults to opus and sends the chosen model in the POST body', async () => {
    const fetchMock = answerOnce('ok', { model: 'fable' })
    vi.stubGlobal('fetch', fetchMock)

    render(<ConsultSheet report={null} entity={null} />)
    fireEvent.click(screen.getByTestId('consult-toggle'))

    const select = screen.getByTestId('consult-model') as HTMLSelectElement
    expect(select.value).toBe('opus')

    fireEvent.change(select, { target: { value: 'fable' } })
    expect(select.value).toBe('fable')
    expect(localStorage.getItem('legba_consult_model')).toBe('fable')

    fireEvent.change(screen.getByTestId('consult-input'), { target: { value: 'q' } })
    fireEvent.click(screen.getByTestId('consult-send'))

    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    const body = JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string)
    expect(body.model).toBe('fable')
  })

  it('restores a persisted plane on mount', () => {
    localStorage.setItem('legba_consult_model', 'core')
    render(<ConsultSheet report={null} entity={null} />)
    fireEvent.click(screen.getByTestId('consult-toggle'))
    expect((screen.getByTestId('consult-model') as HTMLSelectElement).value).toBe('core')
  })
})
