/**
 * X3/D3 — the Inspector's SUBJECT click path (review D §C5: "clicking Brazil
 * in the rail opens the Inspector on the target descriptor: allowed_action_packs,
 * cycle_hop_limit, a 64-hex version").
 *
 *  - a `target` selection opens on the subject's READ STATE first: the last
 *    read (its producer, as-of and verify line), carried-unsupported, the typed
 *    absences — then the descriptor, folded, under "Configuration";
 *  - the subject is called by the profile's noun, never "target";
 *  - absence renders as absence: "—" while the reads are in flight, "not
 *    measured" for a kind the route could not read, never 0.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import InspectorPanel from './InspectorPanel'
import { summarizeAbsences, verifyLine } from './SubjectReadState'
import { useSelection } from '@/state/selection'
import type { PanelRegistration } from '@/types'
import type { AbsenceResponse } from '@/lib/absenceModel'
import profileUiJson from '@/lib/profile_ui.json'
import { subjectWordsIn } from '@/lib/subjectNoun'

const NOUN = profileUiJson.subject.singular.toLowerCase()

function reg(): PanelRegistration {
  return {
    id: 'inspector',
    panel_id: 'system_inspector',
    descriptor_id: '(singleton)',
    descriptor_version: '00000000',
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Inspector',
    mode: 'personal',
    layout_slot: 'system.inspector',
    data_query: {},
    binding: {},
    retired: false,
    created_at: new Date().toISOString(),
    retired_at: null,
  }
}

function wrap(ui: ReactElement): ReactElement {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

const COMPOSITION = {
  data: [
    {
      id: 'c-br',
      analyst_id: 'country_composition',
      target_id: 'country_g20_br',
      produced_at: '2026-10-08T11:30:02Z',
      title: 'Brazil — tariff pressure ahead of the election',
      verification: { faithfulness_score: 0.82, judge_status: 'llm' },
      data: {},
    },
  ],
}

const ABSENCE: AbsenceResponse = {
  version: '2026-10',
  scope: 'country_g20_br',
  read_at: '2026-10-08T12:00:00Z',
  kinds: {},
  absences: [
    {
      kind: 'carried_unsupported',
      subject: 'energy_security',
      since: null,
      window: null,
      reason: 'carried',
      as_of: '2026-10-08T06:00:00Z',
      as_of_basis: 'the unit run',
      expires_at: null,
      stale: false,
      proof: { what_was_checked: 'x', checked_at: null, ref: null, ref_kind: null },
    },
    {
      kind: 'source_stale',
      subject: 'source.agenciabrasil.english',
      since: null,
      window: null,
      reason: 'stale',
      as_of: '2026-10-08T06:00:00Z',
      as_of_basis: 'the receipt',
      expires_at: null,
      stale: false,
      proof: { what_was_checked: 'x', checked_at: null, ref: null, ref_kind: null },
    },
  ],
  not_measured: ['history_gap: no curated collection declares this desk'],
}

const DESCRIPTOR = {
  descriptor_id: 'country_g20_br',
  name: 'G20 — Brazil',
  state: 'active',
  version: '534f405891f217ba638d67736f35522c0c101ea65a8ff82970fa14d0fb97ffde',
  family: 'target',
  body: { coordination: { cycle_hop_limit: 0 }, allowed_action_packs: [{ pack_id: 'media_processing' }] },
}

function mockFetch(opts: { absence?: Promise<unknown> } = {}) {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation((url: string) => {
      if (url.includes('/v3/absence')) {
        if (opts.absence) return opts.absence
        return Promise.resolve({ ok: true, json: async () => ABSENCE })
      }
      if (url.includes('/findings?target_id=country_g20_br') && url.includes('analyst_id_in=')) {
        return Promise.resolve({ ok: true, json: async () => COMPOSITION })
      }
      if (url.includes('/registry/descriptors/target/')) {
        return Promise.resolve({ ok: true, json: async () => DESCRIPTOR })
      }
      return Promise.resolve({ ok: true, json: async () => ({ data: [] }) })
    }),
  )
}

beforeEach(() => {
  localStorage.clear()
  useSelection.getState().clear()
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('the Inspector on a subject — the read first, the configuration one click further', () => {
  it('renders the read state BEFORE a folded Configuration that holds the descriptor', async () => {
    mockFetch()
    render(wrap(<InspectorPanel registration={reg()} scope={{}} mode="personal" />))
    useSelection.getState().select({ kind: 'target', id: 'country_g20_br', label: 'Brazil', origin: 'rail' })

    const read = await screen.findByTestId('inspector-subject-read')
    const config = screen.getByTestId('inspector-configuration')
    // the read comes first in the document; the configuration is folded
    expect(read.compareDocumentPosition(config) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(config.tagName).toBe('DETAILS')
    expect(config).not.toHaveAttribute('open')
    expect(within(config).getByText('Configuration')).toBeInTheDocument()

    // last read: headline, producer + as-of, its verify line
    await waitFor(() => expect(screen.getByTestId('inspector-subject-last-read')).toBeInTheDocument())
    const last = screen.getByTestId('inspector-subject-last-read')
    expect(last).toHaveTextContent('Brazil — tariff pressure ahead of the election')
    expect(last).toHaveTextContent('2026-10-08 11:30Z')
    expect(screen.getByTestId('inspector-subject-verify')).toHaveTextContent('faithfulness 0.82 (judge: llm)')

    // carried-unsupported on its own line; a present kind counted; an unread kind "not measured"
    await waitFor(() =>
      expect(screen.getByTestId('inspector-subject-carried-unsupported')).toHaveTextContent('carried, unsupported'),
    )
    expect(screen.getByTestId('inspector-subject-carried-unsupported')).toHaveTextContent(/^carried, unsupported1 /)
    expect(screen.getByTestId('inspector-subject-absence-source_stale')).toHaveTextContent('1 item')
    expect(screen.getByTestId('inspector-subject-absence-history_gap')).toHaveTextContent('not measured')

    // the descriptor's internals are NOT on the first screen (they sit in the folded details)
    const visible = [...screen.getByTestId('inspector-body').childNodes]
      .filter((n) => n !== config && !(n as Element).contains?.(config))
      .map((n) => n.textContent ?? '')
      .join(' ')
    expect(visible).not.toContain('cycle_hop_limit')
    expect(visible).not.toContain('534f405891f2')
  })

  it("names the subject with the profile's noun — the kind chip, the subtitle — never 'target'", async () => {
    mockFetch()
    render(wrap(<InspectorPanel registration={reg()} scope={{}} mode="personal" />))
    useSelection.getState().select({ kind: 'target', id: 'country_g20_br', label: 'Brazil', origin: 'rail' })
    await screen.findByTestId('inspector-subject-last-read')
    await screen.findByTestId('inspector-subject-carried-unsupported')
    expect(screen.getByTestId('inspector-kind')).toHaveTextContent(NOUN)
    const read = screen.getByTestId('inspector-subject-read').textContent ?? ''
    const chip = screen.getByTestId('inspector-kind').textContent ?? ''
    const words = subjectWordsIn(`${chip} ${read}`)
    expect(words.every((w) => w === NOUN)).toBe(true)
    // the subtitle names the kind by the noun too
    expect(screen.getByText(new RegExp(`^${NOUN} · from rail$`))).toBeInTheDocument()
  })

  it('absence renders as absence: "—" while the absence read is in flight, never 0', async () => {
    mockFetch({ absence: new Promise(() => {}) })
    render(wrap(<InspectorPanel registration={reg()} scope={{}} mode="personal" />))
    useSelection.getState().select({ kind: 'target', id: 'country_g20_br', label: 'Brazil', origin: 'rail' })
    const pending = await screen.findByTestId('inspector-subject-absences-pending')
    expect(pending.textContent).toBe('—')
    expect(screen.getByTestId('inspector-subject-absences').textContent).not.toMatch(/\b0\b/)
  })

  it('a non-subject selection is unchanged: no read-state block, no Configuration fold', async () => {
    mockFetch()
    render(wrap(<InspectorPanel registration={reg()} scope={{}} mode="personal" />))
    useSelection.getState().select({ kind: 'source', id: 'source.38north.analysis', label: '38 North', origin: 'rail' })
    await waitFor(() => expect(screen.getByTestId('inspector-body')).toBeInTheDocument())
    expect(screen.queryByTestId('inspector-subject-read')).toBeNull()
    expect(screen.queryByTestId('inspector-configuration')).toBeNull()
  })
})

describe('the read-state folds, pure', () => {
  it('summarizeAbsences: carried-unsupported counted; unread kinds named; read-empty kinds counted', () => {
    const s = summarizeAbsences(ABSENCE)
    expect(s.carriedUnsupported).toBe(1)
    expect(s.present.map((p) => p.kind)).toEqual(['source_stale', 'carried_unsupported'])
    expect(s.notMeasured).toEqual(['history_gap'])
    expect(s.readEmpty).toBe(9 - 2 - 1)
  })

  it('carried-unsupported the route could not read is null ("not measured"), never 0', () => {
    const s = summarizeAbsences({ ...ABSENCE, absences: [], not_measured: ['carried_unsupported: no cadence'] })
    expect(s.carriedUnsupported).toBeNull()
  })

  it('verifyLine says what the row carries — never an invented score', () => {
    expect(verifyLine(null)).toMatch(/not judged/)
    expect(verifyLine({ judge_status: 'pending' })).toMatch(/not judged/)
    expect(verifyLine({ unverified_reason: { http_status: '429', at: '2026-10-08T06:12:03Z' } })).toBe(
      'unverified since 2026-10-08 06:12Z (judge unavailable: 429)',
    )
  })
})
