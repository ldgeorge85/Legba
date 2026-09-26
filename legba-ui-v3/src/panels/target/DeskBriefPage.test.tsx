/**
 * Component test for the desk brief page (P-A).
 *
 * One routed fetch mock drives the whole surface, so the assertions are about
 * what the page SAYS rather than about how many requests it made:
 *
 *   * every section renders, including its empty / unmeasured state;
 *   * the ambient consult scope pin is set to the desk on open and re-pinned
 *     to a unit's read when the block is opened;
 *   * the JSON action hands the browser the document the route served, byte
 *     for byte — never a re-serialisation of the page's own state;
 *   * the PNG action goes through `html-to-image` (mocked) against the page's
 *     own DOM, and the print action stays on the MARKDOWN document of record.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

const selectRow = vi.fn()
vi.mock('@/state/selection', async () => {
  const actual = await vi.importActual<typeof import('@/state/selection')>('@/state/selection')
  return { ...actual, selectRow: (...args: unknown[]) => selectRow(...args) }
})

const toPng = vi.fn(async (_node: unknown, _opts?: unknown) => 'data:image/png;base64,AAAA')
vi.mock('html-to-image', () => ({
  toPng: (node: unknown, opts?: unknown) => toPng(node, opts),
}))

const printExportDocument = vi.fn((_opts: { markdown: string; filename: string; printedAt: string }) => true)
vi.mock('@/lib/printDocument', () => ({
  printExportDocument: (opts: { markdown: string; filename: string; printedAt: string }) =>
    printExportDocument(opts),
}))

const downloadExportArtifact = vi.fn()
vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api')
  return { ...actual, downloadExportArtifact: (...a: unknown[]) => downloadExportArtifact(...a) }
})

import DeskBriefPagePanel from './DeskBriefPage'
import { resetUnitCorrectnessCache } from '@/lib/unitCorrectnessModel'
import { resetScope, useScope } from '@/state/scope'
import { GAP_STRIP_UNITS } from '@/lib/gapStripModel'
import type { PanelRegistration } from '@/types'

const TARGET = 'country_watch_tw'
const FRESH = new Date(Date.now() - 2 * 3600_000).toISOString()

const REGISTRATION = {
  id: `target.desk_brief_page:${TARGET}`,
  panel_id: 'target_desk_brief_page',
  descriptor_id: TARGET,
  descriptor_version: '00000000',
  descriptor_family: 'target',
  analyst_id: null,
  title: 'Desk Brief',
  mode: 'personal',
  layout_slot: 'target.desk_brief_page',
  data_query: {},
  binding: { target_id: TARGET },
  retired: false,
  created_at: FRESH,
  retired_at: null,
} as unknown as PanelRegistration

const COMPOSITION = {
  id: 'comp-1',
  title: 'country_watch_tw, 2026-09-26 — 2 verified desk reads',
  body: 'The desk read [[ref:1]].',
  analyst_id: 'country_composition',
  target_id: TARGET,
  produced_at: FRESH,
  derived_from: [],
  data: {
    scale_version: 'intensity/2026-08',
    method_version: 'composition_slice/2026-09.1',
    data: {
      assembly: {
        schema: 'assembly.v1',
        regime: 'assembly',
        tier: 'country',
        as_of: FRESH,
        blocks: [
          {
            ordinal: 1,
            finding_id: 'unit-esc',
            desk: 'escalation',
            target_id: TARGET,
            target_name: 'Taiwan',
            question: 'What changed on the escalation ladder?',
            question_source: 'descriptor',
            produced_at: FRESH,
            evidence_age_h: 6,
            tier: 'basis',
            severity: 'medium',
            spans: [
              {
                role: 'bluf',
                text: 'Nothing new was observed.',
                origin: { head_id: 'unit-esc', start: 0, end: 25, body_sha256: 'x', body_len: 25 },
                markers: ['[1]'],
                scope_tokens: [],
              },
            ],
            signals: [],
          },
        ],
        coverage: [
          { unit: 'escalation', unit_name: 'Escalation', status: 'in_basis', read_date: FRESH, age_h: 6 },
          {
            unit: 'energy_security',
            unit_name: 'Energy security',
            status: 'below_floor',
            read_date: FRESH,
            age_h: 8,
          },
        ],
      },
    },
  },
}

const EXPORT_DOC = {
  title: `Desk brief — ${TARGET}`,
  generated_at: '2026-09-26T11:59:00Z',
  item_count: 2,
  missing_count: 0,
  provenance_note: 'Composed from the substrate; every figure carries its record.',
  appendix: null,
  items: [
    {
      kind: 'finding',
      id: 'comp-1',
      row_kind: 'finding',
      title: 'country_watch_tw, 2026-09-26',
      analyst_id: 'country_composition',
      target_id: TARGET,
      severity: 'medium',
      produced_at: FRESH,
      superseded: false,
      body: 'The desk read [[ref:1]].',
      verify_state: 'faithfulness=0.94',
      verify_flags: {},
      citations: [
        {
          marker: '[[ref:1]]',
          citation_kind: 'finding',
          resolves_against: 'analyst_outputs',
          marker_class: null,
          signal_id: null,
          ref_id: 'unit-esc',
          title: 'Escalation read',
          canonical_url: 'escalation',
          resolved: true,
          resolution_source: 'in_row',
        },
      ],
    },
    {
      kind: 'finding',
      id: 'unit-esc',
      row_kind: 'finding',
      title: 'Escalation read',
      analyst_id: 'escalation',
      target_id: TARGET,
      severity: 'medium',
      produced_at: FRESH,
      superseded: false,
      body: 'Nothing new was observed [1].',
      verify_state: 'faithfulness=0.88',
      verify_flags: {},
      citations: [
        {
          marker: '[1]',
          citation_kind: 'signal',
          resolves_against: 'signals',
          marker_class: null,
          signal_id: 'sig-1',
          ref_id: null,
          title: 'Taiwan sees no new Chinese moves',
          canonical_url: 'https://www.example.org/tw',
          resolved: true,
          resolution_source: 'live',
          citation_date: '2026-09-25',
          citation_date_label: 'published',
        },
      ],
    },
  ],
  absences: {
    scope: TARGET,
    read_at: '2026-09-26T11:00:00Z',
    item_count: 1,
    note: 'glossary note',
    by_kind: [
      {
        kind: 'not_collected',
        meaning: 'nothing covers this subject',
        state: 'absent',
        not_measured: null,
        count: 1,
        held_back: 0,
        items: [
          {
            subject: 'energy_security',
            reason: 'no registered source covers this unit for this desk',
            since: null,
            window: null,
            as_of: '2026-09-26T04:00:00Z',
            as_of_basis: 'the last source scan',
            expires_at: null,
            stale: false,
            proof: {
              what_was_checked: 'the desk source map',
              checked_at: '2026-09-26T04:00:00Z',
              ref: null,
              ref_kind: null,
            },
          },
        ],
      },
      {
        kind: 'source_stale',
        meaning: 'a covering source stopped',
        state: 'not_measured',
        not_measured: 'the source-quality read failed for this desk',
        count: 0,
        held_back: 0,
        items: [],
      },
    ],
  },
}

const SCORECARD = [
  {
    target_id: TARGET,
    id: 'sc-1',
    produced_at: '2026-09-26T04:40:00Z',
    generated_at: '2026-09-26T04:40:00Z',
    floors: {},
    dimensions: {
      escalation: {
        band: 'elevated',
        basis: ['unit-esc'],
        severity_tag: 'medium',
        effective_confidence: 0.8,
        confidence: 0.9,
        critic_score: 0.88,
        damped: false,
        reason: 'qualified',
        produced_at: FRESH,
      },
    },
    composition: { present: true, basis: ['comp-1'] },
  },
]

const CORRECTNESS = {
  target_id: TARGET,
  reference: {
    state: 'current',
    id: 'ref-1',
    sha256: 'abc',
    builder: 'core-plane-lane',
    window_start: '2026-09-12T13:43:00Z',
    window_end: '2026-09-26T13:43:00Z',
    span_verified_rate: 0.5,
    thin_dimensions: [],
    age_days: 0.11,
  },
  data: [
    {
      id: 'uc-1',
      analyst_id: 'escalation',
      target_id: TARGET,
      head_id: 'unit-esc',
      as_of: '2026-09-26T02:20:00Z',
      rubric_sha: 'r',
      grain: 'desk',
      n_claims: 11,
      n_contains: 10,
      n_contradicts: 1,
      n_silent: 0,
      n_split: 0,
      n_unparseable: 0,
      n_single_family: 0,
      n_decided: 11,
      correctness_share: 0.909,
      coverage_share: 0.244,
      single_family: false,
      badge: 'correctness 90.9% (10/11) · coverage 24.4% (n=45) · as of 2026-09-26',
      families: {},
      cost_usd: 0,
      created_at: FRESH,
      reference: { state: 'current', id: 'ref-1', sha256: 'abc', builder: 'b', window_start: null, window_end: null, span_verified_rate: null, thin_dimensions: [], age_days: null },
    },
  ],
  next_cursor: null,
}

const DIVERGENCE = {
  measured: true,
  generated_at: FRESH,
  reader_version: 'v',
  unit_sentence: 's',
  as_of: '2026-09-26',
  receipt_run_id: null,
  run_started_at: null,
  method_version: null,
  payload_schema: null,
  classification_audit: null,
  window_days: 14,
  baseline_days: 28,
  z_threshold: 2,
  mad_floor: 1,
  consecutive_days: 2,
  thin_min_per_day: 3,
  layer_vocab: ['official', 'domestic_press'],
  pairs_declared: [],
  desks: [],
  desks_unresolved: [],
  warnings: [],
}

/** One routed mock over every read the page makes. */
function routedFetch(over: { exportBody?: unknown } = {}) {
  return vi.fn(async (url: string, init?: RequestInit) => {
    if (url.includes('/v3/export')) {
      const body = JSON.parse(String(init?.body ?? '{}'))
      const payload =
        body.format === 'json'
          ? JSON.stringify(over.exportBody ?? EXPORT_DOC)
          : '# Desk brief\n\nmarkdown bytes'
      return {
        ok: true,
        headers: {
          get: (k: string) =>
            k === 'Content-Type'
              ? body.format === 'json'
                ? 'application/json'
                : 'text/markdown'
              : `attachment; filename="brief.${body.format === 'json' ? 'json' : 'md'}"`,
        },
        text: async () => payload,
      }
    }
    if (url.includes('country_scorecard')) return { ok: true, json: async () => SCORECARD }
    if (url.includes('/correctness')) return { ok: true, json: async () => CORRECTNESS }
    if (url.includes('/v3/layers/divergence')) return { ok: true, json: async () => DIVERGENCE }
    if (url.includes('analyst_id=country_composition')) {
      return { ok: true, json: async () => ({ data: [COMPOSITION] }) }
    }
    if (url.includes('/situations')) {
      return { ok: true, json: async () => ({ data: [], next_cursor: null }) }
    }
    return { ok: true, json: async () => ({ data: [], next_cursor: null }) }
  })
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

function renderPanel() {
  return render(
    wrap(
      <DeskBriefPagePanel
        registration={REGISTRATION}
        scope={{ target_id: TARGET }}
        mode="personal"
      />,
    ),
  )
}

beforeEach(() => {
  vi.restoreAllMocks()
  selectRow.mockReset()
  toPng.mockClear()
  printExportDocument.mockClear()
  downloadExportArtifact.mockClear()
  resetUnitCorrectnessCache()
  resetScope()
  localStorage.clear()
})

describe('DeskBriefPage', () => {
  it('renders the index card with the composition, its stamps and its two unpooled measurements', async () => {
    vi.stubGlobal('fetch', routedFetch())
    renderPanel()
    expect(await screen.findByTestId('desk-brief-index-card')).toBeInTheDocument()
    expect(await screen.findByTestId('desk-brief-title')).toHaveTextContent('country_watch_tw')
    expect(await screen.findByTestId('desk-brief-faithfulness')).toHaveTextContent('faithfulness=0.94')
    expect(screen.getByTestId('desk-brief-scale-stamp')).toHaveAttribute('data-stamp-state', 'full')
    expect(screen.getByTestId('desk-brief-never-pooled')).toBeInTheDocument()
  })

  it('states the units carried against the ROSTER, not against itself', async () => {
    vi.stubGlobal('fetch', routedFetch())
    renderPanel()
    await waitFor(() =>
      expect(screen.getByTestId('desk-brief-units-carried')).toHaveTextContent(
        `1 of ${GAP_STRIP_UNITS.length} on the roster`,
      ),
    )
  })

  it('renders one block per carried unit, cited, with its own faithfulness', async () => {
    vi.stubGlobal('fetch', routedFetch())
    renderPanel()
    const blocks = await screen.findAllByTestId('desk-brief-unit-block')
    expect(blocks).toHaveLength(1)
    expect(blocks[0]).toHaveAttribute('data-unit', 'escalation')
    expect(await screen.findByTestId('desk-brief-unit-faithfulness')).toHaveTextContent(
      'faithfulness=0.88',
    )
    // The unit's own question appears twice by design — under the block and
    // in its evidence row — so the assertion is on the set, not on one node.
    expect(screen.getAllByText(/What changed on the escalation ladder\?/).length).toBeGreaterThan(0)
  })

  it('pins the consult scope to the desk on open, and to the unit read when a block is opened', async () => {
    vi.stubGlobal('fetch', routedFetch())
    renderPanel()
    await waitFor(() => expect(useScope.getState().scope?.id).toBe(TARGET))
    expect(useScope.getState().scope?.kind).toBe('target')

    fireEvent.click(await screen.findByTestId('desk-brief-unit-open'))
    await waitFor(() => expect(useScope.getState().scope?.id).toBe('unit-esc'))
    expect(useScope.getState().scope?.members.findingIds).toContain('unit-esc')
    expect(selectRow).toHaveBeenCalledWith('finding', 'unit-esc', 'Escalation', {
      origin: 'desk-brief-page',
    })
  })

  it('renders the evidence table over every roster unit, with its state and its typed absence', async () => {
    vi.stubGlobal('fetch', routedFetch())
    renderPanel()
    await screen.findByTestId('desk-brief-evidence')
    for (const unit of GAP_STRIP_UNITS) {
      expect(screen.getByTestId(`desk-brief-evidence-row-${unit.id}`)).toBeInTheDocument()
    }
    await waitFor(() =>
      expect(screen.getByTestId('desk-brief-evidence-row-energy_security')).toHaveAttribute(
        'data-state',
        'below_floor',
      ),
    )
    expect(screen.getByTestId('desk-brief-evidence-row-energy_security')).toHaveTextContent(
      'not collected',
    )
    expect(screen.getByTestId('desk-brief-evidence-row-military_posture')).toHaveAttribute(
      'data-state',
      'no_read',
    )
  })

  it('prints an unmeasured figure as unmeasured, never as a zero', async () => {
    vi.stubGlobal('fetch', routedFetch())
    renderPanel()
    const row = await screen.findByTestId('desk-brief-evidence-row-military_posture')
    expect(row).toHaveTextContent('unmeasured')
    expect(row).not.toHaveTextContent('0%')
  })

  it('states the desk’s layer aperture as undeclared when no layer map names it', async () => {
    vi.stubGlobal('fetch', routedFetch())
    renderPanel()
    await waitFor(() =>
      expect(screen.getByTestId('desk-brief-layers')).toHaveAttribute('data-state', 'undeclared'),
    )
    expect(screen.getByTestId('desk-brief-layers')).toHaveTextContent('No layer map is declared')
  })

  it('renders the endnotes with the route’s own date word kept verbatim', async () => {
    vi.stubGlobal('fetch', routedFetch())
    renderPanel()
    await waitFor(() =>
      expect(screen.getByTestId('desk-brief-endnotes')).toHaveTextContent('published 2026-09-25'),
    )
    expect(screen.getByTestId('desk-brief-endnotes')).toHaveTextContent('example.org')
  })

  it('states an undated citation as an absence rather than printing a blank', async () => {
    const undated = structuredClone(EXPORT_DOC)
    delete (undated.items[1].citations[0] as Record<string, unknown>).citation_date
    delete (undated.items[1].citations[0] as Record<string, unknown>).citation_date_label
    vi.stubGlobal('fetch', routedFetch({ exportBody: undated }))
    renderPanel()
    await waitFor(() =>
      expect(screen.getByTestId('desk-brief-endnotes')).toHaveTextContent('no date on record'),
    )
  })

  it('generates "what this page does not publish" from the records, not from prose', async () => {
    vi.stubGlobal('fetch', routedFetch())
    renderPanel()
    await waitFor(() =>
      expect(screen.getByTestId('desk-brief-not-published')).toHaveTextContent(
        'the source-quality read failed for this desk',
      ),
    )
    expect(screen.getByTestId('desk-brief-not-published')).toHaveTextContent('[typed absence]')
  })

  it('carries every reading limit the records state', async () => {
    vi.stubGlobal('fetch', routedFetch())
    renderPanel()
    await waitFor(() =>
      expect(screen.getByTestId('desk-brief-reading-limits')).toHaveTextContent(
        '2026-09-26T11:00:00Z',
      ),
    )
    const limits = screen.getByTestId('desk-brief-reading-limits')
    expect(limits).toHaveTextContent('intensity/2026-08')
    expect(limits).toHaveTextContent('current · window 2026-09-12 → 2026-09-26')
  })

  it('hands the JSON action the document the route served, byte for byte', async () => {
    vi.stubGlobal('fetch', routedFetch())
    renderPanel()
    const button = await screen.findByTestId('desk-brief-page-json')
    await waitFor(() => expect(button).not.toBeDisabled())
    fireEvent.click(button)
    await waitFor(() => expect(downloadExportArtifact).toHaveBeenCalledTimes(1))
    const artifact = downloadExportArtifact.mock.calls[0][0] as { content: string }
    expect(JSON.parse(artifact.content).items).toHaveLength(2)
  })

  it('captures the PNG off the page’s own DOM, and leaves print on the markdown document', async () => {
    vi.stubGlobal('fetch', routedFetch())
    renderPanel()
    const png = await screen.findByTestId('desk-brief-page-png')
    await waitFor(() => expect(png).not.toBeDisabled())
    fireEvent.click(png)
    await waitFor(() => expect(toPng).toHaveBeenCalledTimes(1))
    expect(toPng.mock.calls[0][0]).toBeInstanceOf(HTMLElement)

    fireEvent.click(await screen.findByTestId('desk-brief-page-print'))
    await waitFor(() => expect(printExportDocument).toHaveBeenCalledTimes(1))
    expect(printExportDocument.mock.calls[0][0].markdown).toContain('markdown bytes')
  })

  it('says a desk with no composition has none, and still states its whole roster', async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url.includes('country_scorecard')) return { ok: true, json: async () => [] }
      if (url.includes('/correctness')) {
        return { ok: true, json: async () => ({ target_id: TARGET, reference: { state: 'none' }, data: [], next_cursor: null }) }
      }
      if (url.includes('/v3/layers/divergence')) return { ok: true, json: async () => DIVERGENCE }
      return { ok: true, json: async () => ({ data: [], next_cursor: null }) }
    })
    vi.stubGlobal('fetch', fetchMock)
    renderPanel()
    expect(await screen.findByTestId('desk-brief-no-composition')).toBeInTheDocument()
    for (const unit of GAP_STRIP_UNITS) {
      expect(screen.getByTestId(`desk-brief-evidence-row-${unit.id}`)).toHaveAttribute(
        'data-state',
        'no_read',
      )
    }
    expect(screen.getByTestId('desk-brief-endnotes-empty')).toBeInTheDocument()
  })
})
