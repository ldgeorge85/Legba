/**
 * Component test for the `system.journal_gate` panel — THE HUMAN GATE.
 *
 * Mocks the registry at the HTTP boundary and routes by URL + method:
 *   GET  /journal_proposals              → the queue
 *   POST /journal_proposals/{id}/accept  → the apply
 *   POST /journal_proposals/{id}/reject  → the refusal (reason required)
 *
 * What these tests exist to hold:
 *   * the queue lists, and each row leads with WHAT ACCEPTING WOULD DO;
 *   * accept is two-click (one click never writes) and only then POSTs;
 *   * reject cannot fire without a reason, and sends the typed one;
 *   * a failed decision surfaces the SERVER's own error and never reports
 *     success — the 409 protected-section auto-reject is the case that matters,
 *     because it is the one where a naive panel would show a green tick;
 *   * a replayed decision says nothing was re-applied.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import JournalGatePanel from './JournalGate'
import { mockErrorResponse } from '@/test/apiMocks'
import type { JournalProposal } from '@/lib/api'
import type { PanelRegistration } from '@/types'

function reg(): PanelRegistration {
  return {
    id: 'g1',
    panel_id: 'system_journal_gate',
    descriptor_id: '(singleton)',
    descriptor_version: '0'.repeat(64),
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Journal Gate',
    mode: 'personal',
    layout_slot: 'main',
    data_query: {},
    binding: {},
    retired: false,
    created_at: '2026-08-01T00:00:00Z',
    retired_at: null,
  }
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

const CORRECTION: JournalProposal = {
  id: 'p-correction',
  proposal_kind: 'correction',
  proposed_by_analyst_id: 'journal_assessor',
  run_id: null,
  rationale: 'The cited signal retired this value three weeks ago.',
  diff: {
    op: 'supersede_fact',
    subject: 'Wagner Group',
    predicate: 'commander',
    value: 'Pavel Prigozhin',
  },
  cited_substrate_refs: ['11111111-1111-1111-1111-111111111111'],
  status: 'pending',
  decided_by: null,
  decision_reason: null,
  decided_at: null,
  produced_at: '2026-08-19T00:00:00Z',
  self_revision_evidence: null,
}

/** A self-revision that DROPS two protected clauses — the server would 409 it. */
const UNSAFE_SELF_REVISION: JournalProposal = {
  id: 'p-self',
  proposal_kind: 'self_revision',
  proposed_by_analyst_id: 'journal_assessor',
  run_id: 'run-9',
  rationale: 'A tighter voice would serve the reader better.',
  diff: {
    target_analyst_id: 'journal_assessor',
    new_prompt_text: 'Poetry without evidence is noise. Cite as [[ref:x]]. Never re-assert retired state.',
  },
  cited_substrate_refs: [],
  status: 'pending',
  decided_by: null,
  decision_reason: null,
  decided_at: null,
  produced_at: '2026-08-19T01:00:00Z',
  self_revision_evidence: {
    available: true,
    forecast_unproven: true,
    calibration_thin: true,
    brier_skill_score: null,
    journal_critic_mean: 0.62,
    journal_critic_n: 7,
  },
}

interface PostCall {
  url: string
  body: unknown
}

let posts: PostCall[] = []
/** Per-URL-fragment override for the next POST response. */
let postResponder: ((url: string) => Response | null) | null = null

function mockFetch(proposals: JournalProposal[] = [CORRECTION, UNSAFE_SELF_REVISION]) {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const u = String(url)
    if (init?.method === 'POST') {
      posts.push({ url: u, body: init.body ? JSON.parse(String(init.body)) : null })
      const override = postResponder?.(u)
      if (override) return override
      const id = /journal_proposals\/([^/]+)\//.exec(u)?.[1] ?? 'x'
      const accepted = u.endsWith('/accept')
      return {
        ok: true,
        json: async () => ({
          id,
          status: accepted ? 'accepted' : 'rejected',
          decided_by: 'operator',
          decision_reason: accepted ? null : 'no good',
          applied: accepted ? { op: 'supersede_fact', facts_superseded: 1 } : null,
          replayed: false,
        }),
      } as unknown as Response
    }
    if (u.includes('/journal_proposals')) {
      return { ok: true, json: async () => ({ proposals }) } as unknown as Response
    }
    return { ok: true, json: async () => ({}) } as unknown as Response
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  posts = []
  postResponder = null
})

function renderPanel() {
  return render(wrap(<JournalGatePanel registration={reg()} scope={{}} mode="personal" />))
}

describe('JournalGatePanel — the queue', () => {
  it('lists pending proposals off GET /journal_proposals?status=pending', async () => {
    const f = mockFetch()
    renderPanel()
    await waitFor(() => {
      expect(screen.getByTestId('journal-gate-row-p-correction')).toBeInTheDocument()
    })
    expect(screen.getByTestId('journal-gate-row-p-self')).toBeInTheDocument()
    expect(String(f.mock.calls[0][0])).toContain('status=pending')
  })

  it('leads each row with the EFFECT of accepting, not the rationale', async () => {
    mockFetch()
    renderPanel()
    const row = await screen.findByTestId('journal-gate-row-p-correction')
    // The collapsed row states the consequence…
    expect(row.textContent).toContain('Closes the open fact')
    // …and NOT the journal's argument for it (that is one expand away).
    expect(row.textContent).not.toContain('retired this value three weeks ago')
  })

  it('renders the honest empty state when nothing awaits a decision', async () => {
    mockFetch([])
    renderPanel()
    expect(await screen.findByTestId('journal-gate-empty')).toHaveTextContent(
      /Nothing is waiting on you/i,
    )
  })

  it('surfaces a queue read failure instead of rendering an empty gate', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => mockErrorResponse(500, { detail: 'pg down' })),
    )
    renderPanel()
    expect(await screen.findByTestId('journal-gate-load-error')).toHaveTextContent(/pg down/)
  })
})

describe('JournalGatePanel — expanding a proposal', () => {
  it('shows the rationale, the §7.5(a) track record, and the proposed prompt verbatim', async () => {
    mockFetch()
    renderPanel()
    await screen.findByTestId('journal-gate-row-p-self')
    fireEvent.click(screen.getByTestId('journal-gate-toggle-p-self'))

    expect(await screen.findByTestId('journal-gate-evidence-p-self')).toHaveTextContent(
      /forecast skill UNPROVEN/,
    )
    expect(screen.getByTestId('journal-gate-body-p-self').textContent).toContain(
      'Poetry without evidence is noise',
    )
    expect(screen.getByTestId('journal-gate-row-p-self').textContent).toContain(
      'A tighter voice would serve the reader better.',
    )
  })

  it('flags a self-revision the server would AUTO-REJECT before the click', async () => {
    mockFetch()
    renderPanel()
    await screen.findByTestId('journal-gate-row-p-self')
    // The warning is visible on the COLLAPSED row — no expand required.
    expect(screen.getByTestId('journal-gate-protected-flag-p-self')).toHaveTextContent(
      /would auto-reject/i,
    )
    fireEvent.click(screen.getByTestId('journal-gate-toggle-p-self'))
    const detail = await screen.findByTestId('journal-gate-protected-p-self')
    expect(detail.textContent).toContain('never write a fact')
    expect(detail.textContent).toContain('the forecast pilot has no skill')
  })
})

describe('JournalGatePanel — accept', () => {
  it('does NOT write on the first click; the button asks for confirmation', async () => {
    mockFetch()
    renderPanel()
    await screen.findByTestId('journal-gate-row-p-correction')
    fireEvent.click(screen.getByTestId('journal-gate-toggle-p-correction'))

    fireEvent.click(await screen.findByTestId('journal-gate-accept-p-correction'))
    expect(posts).toHaveLength(0)
    expect(screen.getByTestId('journal-gate-accept-p-correction')).toHaveTextContent(
      /click again to apply/i,
    )
  })

  it('POSTs the accept on the second click and reports what was applied', async () => {
    mockFetch()
    renderPanel()
    await screen.findByTestId('journal-gate-row-p-correction')
    fireEvent.click(screen.getByTestId('journal-gate-toggle-p-correction'))

    const btn = await screen.findByTestId('journal-gate-accept-p-correction')
    fireEvent.click(btn)
    fireEvent.click(btn)

    await waitFor(() => expect(posts).toHaveLength(1))
    expect(posts[0].url).toContain('/journal_proposals/p-correction/accept')
    expect(await screen.findByTestId('journal-gate-outcome-p-correction')).toHaveTextContent(
      /Accepted and applied \(supersede_fact\)/,
    )
  })

  // --- GLASS-3: the accept side of the decision trail ---------------------
  // Before this, only a reject could carry a reason, so an APPLIED change — the
  // half that actually mutates the substrate — was silent about why by
  // construction.

  it('offers a reason box between the two clicks, and it is OPTIONAL', async () => {
    mockFetch()
    renderPanel()
    await screen.findByTestId('journal-gate-row-p-correction')
    fireEvent.click(screen.getByTestId('journal-gate-toggle-p-correction'))

    const btn = await screen.findByTestId('journal-gate-accept-p-correction')
    // Not offered before the operator has committed to accepting.
    expect(
      screen.queryByTestId('journal-gate-accept-reason-p-correction'),
    ).not.toBeInTheDocument()

    fireEvent.click(btn)
    expect(
      screen.getByTestId('journal-gate-accept-reason-p-correction'),
    ).toBeInTheDocument()

    // Leaving it empty must NOT block the apply — unlike reject's.
    fireEvent.click(btn)
    await waitFor(() => expect(posts).toHaveLength(1))
    expect(posts[0].body).toEqual({})
  })

  it('sends the typed accept reason', async () => {
    mockFetch()
    renderPanel()
    await screen.findByTestId('journal-gate-row-p-correction')
    fireEvent.click(screen.getByTestId('journal-gate-toggle-p-correction'))

    const btn = await screen.findByTestId('journal-gate-accept-p-correction')
    fireEvent.click(btn)
    fireEvent.change(screen.getByTestId('journal-gate-accept-reason-p-correction'), {
      target: { value: '  checked against the cited signal  ' },
    })
    fireEvent.click(btn)

    await waitFor(() => expect(posts).toHaveLength(1))
    expect(posts[0].body).toEqual({
      decision_reason: 'checked against the cited signal',
    })
  })

  it('sends no reason key when the operator typed only whitespace', async () => {
    // Blank must reach the server as "none given", not as an empty string that
    // every IS NOT NULL check downstream would read as a reason.
    mockFetch()
    renderPanel()
    await screen.findByTestId('journal-gate-row-p-correction')
    fireEvent.click(screen.getByTestId('journal-gate-toggle-p-correction'))

    const btn = await screen.findByTestId('journal-gate-accept-p-correction')
    fireEvent.click(btn)
    fireEvent.change(screen.getByTestId('journal-gate-accept-reason-p-correction'), {
      target: { value: '   ' },
    })
    fireEvent.click(btn)

    await waitFor(() => expect(posts).toHaveLength(1))
    expect(posts[0].body).toEqual({})
  })

  it('says plainly that a replayed accept re-applied nothing', async () => {
    mockFetch()
    postResponder = (u) =>
      u.endsWith('/accept')
        ? ({
            ok: true,
            json: async () => ({
              id: 'p-correction',
              status: 'accepted',
              decided_by: 'operator',
              decision_reason: null,
              applied: null,
              replayed: true,
            }),
          } as unknown as Response)
        : null
    renderPanel()
    await screen.findByTestId('journal-gate-row-p-correction')
    fireEvent.click(screen.getByTestId('journal-gate-toggle-p-correction'))
    const btn = await screen.findByTestId('journal-gate-accept-p-correction')
    fireEvent.click(btn)
    fireEvent.click(btn)

    expect(await screen.findByTestId('journal-gate-outcome-p-correction')).toHaveTextContent(
      /nothing was re-applied/i,
    )
  })
})

describe('JournalGatePanel — reject', () => {
  it('will not submit without a reason (the API requires one)', async () => {
    mockFetch()
    renderPanel()
    await screen.findByTestId('journal-gate-row-p-correction')
    fireEvent.click(screen.getByTestId('journal-gate-toggle-p-correction'))
    fireEvent.click(await screen.findByTestId('journal-gate-reject-open-p-correction'))

    const submit = await screen.findByTestId('journal-gate-reject-submit-p-correction')
    expect(submit).toBeDisabled()
    fireEvent.click(submit)
    expect(posts).toHaveLength(0)

    // Whitespace is not a reason either.
    fireEvent.change(screen.getByTestId('journal-gate-reason-p-correction'), {
      target: { value: '   ' },
    })
    expect(screen.getByTestId('journal-gate-reject-submit-p-correction')).toBeDisabled()
  })

  it('POSTs the typed reason and reports the rejection', async () => {
    mockFetch()
    renderPanel()
    await screen.findByTestId('journal-gate-row-p-correction')
    fireEvent.click(screen.getByTestId('journal-gate-toggle-p-correction'))
    fireEvent.click(await screen.findByTestId('journal-gate-reject-open-p-correction'))

    fireEvent.change(screen.getByTestId('journal-gate-reason-p-correction'), {
      target: { value: '  the cited signal does not say this  ' },
    })
    fireEvent.click(screen.getByTestId('journal-gate-reject-submit-p-correction'))

    await waitFor(() => expect(posts).toHaveLength(1))
    expect(posts[0].url).toContain('/journal_proposals/p-correction/reject')
    expect(posts[0].body).toEqual({
      decision_reason: 'the cited signal does not say this',
    })
    expect(await screen.findByTestId('journal-gate-outcome-p-correction')).toHaveTextContent(
      /Rejected/,
    )
  })
})

describe('JournalGatePanel — error paths never read as success', () => {
  it('surfaces a 409 protected-section auto-reject and reports NO outcome', async () => {
    mockFetch()
    postResponder = (u) =>
      u.endsWith('/accept')
        ? mockErrorResponse(409, {
            detail:
              'self_revision auto-rejected (protected section): Dropped clauses: ' +
              "['never write a fact']",
          })
        : null
    renderPanel()
    await screen.findByTestId('journal-gate-row-p-self')
    fireEvent.click(screen.getByTestId('journal-gate-toggle-p-self'))
    const btn = await screen.findByTestId('journal-gate-accept-p-self')
    fireEvent.click(btn)
    fireEvent.click(btn)

    const err = await screen.findByTestId('journal-gate-error-p-self')
    expect(err).toHaveTextContent(/Auto-rejected/)
    expect(err).toHaveTextContent(/NOTHING was applied/)
    expect(screen.queryByTestId('journal-gate-outcome-p-self')).not.toBeInTheDocument()
  })

  it('surfaces a 422 apply failure as a failure, not a partial success', async () => {
    mockFetch()
    postResponder = (u) =>
      u.endsWith('/accept')
        ? mockErrorResponse(422, { detail: 'apply failed: unknown correction op' })
        : null
    renderPanel()
    await screen.findByTestId('journal-gate-row-p-correction')
    fireEvent.click(screen.getByTestId('journal-gate-toggle-p-correction'))
    const btn = await screen.findByTestId('journal-gate-accept-p-correction')
    fireEvent.click(btn)
    fireEvent.click(btn)

    const err = await screen.findByTestId('journal-gate-error-p-correction')
    expect(err).toHaveTextContent(/Apply failed/)
    expect(err).toHaveTextContent(/unknown correction op/)
    expect(screen.queryByTestId('journal-gate-outcome-p-correction')).not.toBeInTheDocument()
  })

  it('surfaces a failed reject too', async () => {
    mockFetch()
    postResponder = (u) =>
      u.endsWith('/reject') ? mockErrorResponse(404, { detail: 'proposal not found' }) : null
    renderPanel()
    await screen.findByTestId('journal-gate-row-p-correction')
    fireEvent.click(screen.getByTestId('journal-gate-toggle-p-correction'))
    fireEvent.click(await screen.findByTestId('journal-gate-reject-open-p-correction'))
    fireEvent.change(screen.getByTestId('journal-gate-reason-p-correction'), {
      target: { value: 'stale' },
    })
    fireEvent.click(screen.getByTestId('journal-gate-reject-submit-p-correction'))

    expect(await screen.findByTestId('journal-gate-error-p-correction')).toHaveTextContent(
      /not found/i,
    )
  })
})

describe('JournalGatePanel — already-decided rows', () => {
  it('offers no accept/reject on a decided proposal and explains why', async () => {
    mockFetch([
      {
        ...CORRECTION,
        status: 'rejected',
        decided_by: 'operator',
        decision_reason: 'superseded already',
        decided_at: '2026-08-19T02:00:00Z',
      },
    ])
    renderPanel()
    await screen.findByTestId('journal-gate-row-p-correction')
    fireEvent.click(screen.getByTestId('journal-gate-toggle-p-correction'))

    expect(await screen.findByTestId('journal-gate-decided-p-correction')).toHaveTextContent(
      /without re-applying/i,
    )
    expect(
      screen.queryByTestId('journal-gate-accept-p-correction'),
    ).not.toBeInTheDocument()
  })

  it('shows the recorded reason on a decided row', async () => {
    mockFetch([
      {
        ...CORRECTION,
        status: 'accepted',
        decided_by: 'operator',
        decision_reason: 'corroborated upstream',
        decided_at: '2026-08-19T02:00:00Z',
      },
    ])
    renderPanel()
    await screen.findByTestId('journal-gate-row-p-correction')
    fireEvent.click(screen.getByTestId('journal-gate-toggle-p-correction'))

    expect(
      await screen.findByTestId('journal-gate-decision-reason-p-correction'),
    ).toHaveTextContent('corroborated upstream')
  })

  it('says "no reason recorded" rather than rendering nothing', async () => {
    // A decided row with no reason is a real, permitted state now that accept's
    // reason is optional. Rendering nothing would let it read as "not
    // applicable" — which is what the pre-GLASS-3 asymmetry looked like.
    mockFetch([
      {
        ...CORRECTION,
        status: 'accepted',
        decided_by: 'operator',
        decision_reason: null,
        decided_at: '2026-08-19T02:00:00Z',
      },
    ])
    renderPanel()
    await screen.findByTestId('journal-gate-row-p-correction')
    fireEvent.click(screen.getByTestId('journal-gate-toggle-p-correction'))

    expect(
      await screen.findByTestId('journal-gate-decision-reason-p-correction'),
    ).toHaveTextContent(/no reason recorded/i)
  })

  it('re-queries when the status filter changes', async () => {
    const f = mockFetch([])
    renderPanel()
    await screen.findByTestId('journal-gate-empty')
    fireEvent.click(screen.getByTestId('journal-gate-filter-accepted'))
    await waitFor(() => {
      expect(f.mock.calls.some((c) => String(c[0]).includes('status=accepted'))).toBe(true)
    })
  })
})
