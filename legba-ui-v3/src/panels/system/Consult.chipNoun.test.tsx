/**
 * X4/D4 (review D C9) — the Consult context chip names a SUBJECT by the
 * profile's subject noun, never by the registry family's word `target`.
 * A pin of any other kind keeps its own word.
 */
import { beforeEach, describe, expect, it } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import ConsultPanel from './Consult'
import { consultActions, useConsultSessions } from '@/state/consultSession'
import { resetScope } from '@/state/scope'
import { useSelection } from '@/state/selection'
import { SUBJECT_NOUN } from '@/lib/subjectNoun'
import type { PanelRegistration } from '@/types'

const reg: PanelRegistration = {
  id: 'c1',
  panel_id: 'system_consult',
  descriptor_id: 'consult.daily',
  descriptor_version: 'v' + 'a'.repeat(63),
  descriptor_family: 'analyst',
  analyst_id: 'consult.daily',
  title: 'Consult',
  mode: 'personal',
  layout_slot: 'system.consult.main',
  data_query: {},
  binding: {},
  retired: false,
  created_at: '2026-05-20T00:00:00Z',
  retired_at: null,
}

beforeEach(() => {
  localStorage.clear()
  useConsultSessions.setState({ panels: {} })
  resetScope()
  useSelection.getState().clear()
})

describe('the Consult context chip noun', () => {
  it('a subject pin reads "<profile noun>:", not "target:"', async () => {
    render(<ConsultPanel registration={reg} scope={{}} mode="personal" />)
    act(() => consultActions().addPin('c1', { kind: 'target', id: 'country_g20_br', label: 'Brazil' }))
    await waitFor(() => expect(screen.getByTestId('consult-pin-chip')).toBeInTheDocument())
    const chip = screen.getByTestId('consult-pin-chip')
    expect(chip.textContent ?? '').toContain(`${SUBJECT_NOUN.toLowerCase()}:`)
    expect(chip.textContent ?? '').not.toMatch(/\btarget:/)
    expect(chip.getAttribute('title')).toBe(`${SUBJECT_NOUN.toLowerCase()} country_g20_br`)
  })

  it('a pin of another kind keeps its own word', async () => {
    render(<ConsultPanel registration={reg} scope={{}} mode="personal" />)
    act(() => consultActions().addPin('c1', { kind: 'finding', id: 'f-1', label: 'Finding One' }))
    await waitFor(() => expect(screen.getByTestId('consult-pin-chip')).toBeInTheDocument())
    expect(screen.getByTestId('consult-pin-chip').textContent ?? '').toContain('finding:')
  })
})
