/**
 * Component test for the Inspector (redesign Move 1 — the keystone).
 *
 * Verifies the selection→detail contract: a finding selection loads its lineage
 * (the reused Why fetch), renders the header / core / body / DERIVED-FROM refs,
 * and that clicking a ref RecordLink re-selects (drill-through). Empty selection
 * renders the world-assessment one-pager (never dead space).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import InspectorPanel from './InspectorPanel'
import { useSelection } from '@/state/selection'
import type { PanelRegistration } from '@/types'

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

const FINDING_LINEAGE = {
  root: {
    id: 'f1',
    row_kind: 'finding',
    title: 'Port closure, Santos',
    produced_at: '2026-06-16T09:12:00Z',
    target_id: 'brazil',
    analyst_id: 'country_assessor',
    schema_uri: 'legba/finding/v1',
    depth: 0,
  },
  nodes: [
    {
      id: 'sig-7',
      row_kind: 'signal',
      title: 'USGS feed item',
      produced_at: '2026-06-16T08:00:00Z',
      target_id: null,
      analyst_id: null,
      schema_uri: 'legba/signal/v1',
      depth: 1,
    },
  ],
  edges: [{ parent: 'sig-7', child: 'f1' }],
}

/**
 * 7b/k5 — the typed-absence payload for the "Why absent" drill. One item that
 * is STALE (its measurer was due to repeat and did not) so the panel has to
 * state it as last known rather than as a current absence.
 */
const ABSENCE = {
  version: '2026-09/k5',
  scope: 'israel',
  read_at: '2026-09-24T12:00:00.000Z',
  kinds: { source_stale: 'the producer is past its own cadence budget' },
  absences: [
    {
      kind: 'source_stale',
      subject: 'energy_security',
      since: '2026-09-22T20:00:00.000Z',
      window: null,
      reason: 'the latest read is 40.0h old, past this unit’s own 12.0h cadence',
      as_of: '2026-09-22T20:00:00.000Z',
      as_of_basis: "this unit's last run for this desk",
      expires_at: '2026-09-23T01:00:00.000Z',
      review: null,
      stale: true,
      proof: {
        what_was_checked:
          "the latest kind='finding' row for analyst 'energy_security' on this desk",
        checked_at: '2026-09-22T20:00:00.000Z',
        ref: 'f-es',
        ref_kind: 'finding',
      },
    },
  ],
  not_measured: [
    'below_floor: no banded scorecard has been computed for this desk yet',
  ],
}

/**
 * 7a/0222 — the contrary-evidence drill, with THE FOUR FENCES on it.
 *
 * A contradiction is not "a page said so": it is a page that was not an
 * encyclopedia entry (F1), was dated inside the claim's window (F2), carried the
 * claim's subject in the matched sentence (F3), and had a second independent
 * outlet behind it (F4). This record cleared all four with two pages; the second
 * fixture below cleared none of them because it predates the columns.
 */
const CONTENTION_FENCED = {
  version: '2026-09/7a.2',
  note: 'a stance is a RETRIEVAL outcome, never a verdict on the claim',
  scope: null,
  scope_field: null,
  stances: ['contradicts'],
  contentions: [
    {
      claim_id: 'c'.repeat(64),
      claim_text: 'The strait remains effectively shut.',
      finding_id: 'f1',
      origin_head_id: null,
      block_ordinal: 2,
      span_role: 'bluf',
      target_id: 'country_watch_ir',
      desk_key: 'country_watch_ir',
      analyst_id: 'country_composition',
      query: 'hormuz reopened operating resumed',
      query_source: 'polarity',
      query_novel_tokens: 3,
      stance: 'contradicts',
      derivation: 'polarity',
      reason: '',
      statement: 'A page this platform holds states the opposite.',
      rung: 'searxng',
      refs: [
        {
          url: 'https://news.example/hormuz-reopens',
          sha256: 'ab'.repeat(32),
          chars: 900,
          published_at: '2026-09-20',
          extracted: true,
          fetched_at: '2026-09-25T05:37:00+00:00',
          status_code: 200,
          stance: 'contradicts',
          quote: 'Shipping sources said the strait reopened on Tuesday.',
          host_class: 'reporting',
          page_published_at: '2026-09-20',
          subject_overlap: 4,
          fence: '',
        },
        {
          url: 'https://en.wikipedia.org/wiki/Strait_of_Hormuz',
          sha256: 'cd'.repeat(32),
          chars: 14144,
          published_at: '2018-12-10',
          extracted: true,
          fetched_at: '2026-09-25T05:37:00+00:00',
          status_code: 200,
          stance: 'none_found',
          quote: '',
          host_class: 'reference',
          page_published_at: '2018-12-10',
          subject_overlap: 2,
          fence: 'host_class',
        },
      ],
      linked_signals: 0,
      host_class: 'reporting',
      page_published_at: '2026-09-20',
      subject_overlap: 4,
      independent_pages: 2,
      pipeline_version: '2026-09/7a.2',
      retrieved_at: '2026-09-25T05:37:00+00:00',
      as_of: '2026-09-25T05:37:00+00:00',
      expires_at: '2026-10-02T05:37:00+00:00',
      live: true,
    },
  ],
}

/** The same route, a row written BEFORE migration 0222: all four fence columns
 *  are `null`, and its shelf life has run out. */
const CONTENTION_UNFENCED = {
  ...CONTENTION_FENCED,
  contentions: [
    {
      ...CONTENTION_FENCED.contentions[0],
      stance: 'qualifies',
      refs: [
        {
          ...CONTENTION_FENCED.contentions[0].refs[0],
          host_class: '',
          page_published_at: null,
          subject_overlap: 0,
          fence: '',
        },
      ],
      host_class: null,
      page_published_at: null,
      subject_overlap: null,
      independent_pages: null,
      pipeline_version: '2026-09/7a.1',
      live: false,
    },
  ],
}

/** Route fetch by URL so the lineage walk + assessment poll both resolve. */
function mockFetch(contentions: unknown = CONTENTION_FENCED) {
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    if (url.includes('/v3/contentions')) {
      return Promise.resolve({ ok: true, json: async () => contentions })
    }
    if (url.includes('/v3/absence')) {
      return Promise.resolve({ ok: true, json: async () => ABSENCE })
    }
    if (url.includes('/lineage/finding/f1')) {
      return Promise.resolve({ ok: true, json: async () => FINDING_LINEAGE })
    }
    if (url.includes('/lineage/')) {
      return Promise.resolve({ ok: true, json: async () => FINDING_LINEAGE })
    }
    // world-assessment poll + anything else → empty.
    return Promise.resolve({ ok: true, json: async () => ({ data: [] }) })
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('InspectorPanel', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    localStorage.clear()
    useSelection.getState().clear()
  })

  it('renders a call-to-action when nothing is selected (#90: assessment is a finding, not the empty-state)', async () => {
    mockFetch()
    render(wrap(<InspectorPanel registration={reg()} scope={{}} mode="personal" />))
    // Empty state = a plain CTA. The world assessment is no longer special-cased
    // here — it's a finding read in the Inspector like any other when selected.
    await waitFor(() =>
      expect(screen.queryByTestId('inspector-empty')).toBeInTheDocument(),
    )
  })

  it('loads a finding selection: header, body, and DERIVED-FROM ref', async () => {
    mockFetch()
    render(wrap(<InspectorPanel registration={reg()} scope={{}} mode="personal" />))
    useSelection.getState().select({ kind: 'finding', id: 'f1', label: 'Port closure', origin: 'feed' })

    // The lineage root title surfaces as the body title.
    await waitFor(() => expect(screen.getByTestId('inspector-refs')).toBeInTheDocument())

    // DERIVED FROM lists the parent signal as a RecordLink.
    const refs = screen.getByTestId('inspector-refs')
    expect(refs).toHaveTextContent('USGS feed item')

    // The Inspector body carries the resolved record id in its header.
    const body = screen.getByTestId('inspector-body')
    expect(body).toHaveTextContent('f1')
  })

  it('clicking a DERIVED-FROM ref re-selects (drill-through)', async () => {
    mockFetch()
    render(wrap(<InspectorPanel registration={reg()} scope={{}} mode="personal" />))
    useSelection.getState().select({ kind: 'finding', id: 'f1', label: 'Port closure' })
    await waitFor(() => expect(screen.getByTestId('inspector-refs')).toBeInTheDocument())

    // The ref link selects the upstream signal.
    const link = screen.getByTestId('inspector-refs').querySelector('[data-testid="record-link"]')!
    fireEvent.click(link)

    const sel = useSelection.getState().selection
    expect(sel?.kind).toBe('signal')
    expect(sel?.id).toBe('sig-7')
    // Drilling pushed the finding onto the breadcrumb.
    expect(useSelection.getState().history.map((h) => h.id)).toContain('f1')
  })

  // 7b/k5 — the typed-absence drill. The selection whose RECORD DOES NOT EXIST
  // still resolves to something a reader can interrogate: the kind, the proof,
  // and whether anyone has looked since.
  it('an absence selection renders its proof and says it was not re-checked', async () => {
    mockFetch()
    render(wrap(<InspectorPanel registration={reg()} scope={{}} mode="personal" />))
    useSelection.getState().select({
      kind: 'absence',
      id: 'israel|source_stale|energy_security',
      label: 'Energy security — why absent',
      origin: 'desk-gap-strip',
    })

    const body = await screen.findByTestId('inspector-body')
    await waitFor(() => expect(body).toHaveTextContent('source stale'))
    // The PROOF — what was looked at, and the record of that look.
    expect(body).toHaveTextContent("the latest kind='finding' row")
    // The CLOCK — a stale item is last known, never a current absence.
    expect(body).toHaveTextContent('last known absence, not re-checked')
    expect(body).toHaveTextContent("this unit's last run for this desk")
    // The ref the route typed as a finding is offered as a drill-through.
    expect(screen.getByTestId('inspector-refs')).toHaveTextContent('f-es')
  })

  it('an absence the route did not measure says so, and never renders blank', async () => {
    mockFetch()
    render(wrap(<InspectorPanel registration={reg()} scope={{}} mode="personal" />))
    useSelection.getState().select({
      kind: 'absence',
      id: 'israel|below_floor|escalation',
      label: 'Escalation — why absent',
      origin: 'desk-gap-strip',
    })

    const body = await screen.findByTestId('inspector-body')
    await waitFor(() => expect(body).toHaveTextContent('not measured'))
    expect(body).toHaveTextContent('no banded scorecard has been computed')
    // "not checked" is not "nothing absent" — the other sentence must not show.
    expect(body).not.toHaveTextContent('no typed absence is recorded')
  })

  it('an absence that was checked and found nothing says THAT instead', async () => {
    mockFetch()
    render(wrap(<InspectorPanel registration={reg()} scope={{}} mode="personal" />))
    useSelection.getState().select({
      kind: 'absence',
      // Read cleanly (not in `not_measured`), no item for this subject.
      id: 'israel|source_stale|military_posture',
      label: 'Military posture — why absent',
      origin: 'desk-gap-strip',
    })

    const body = await screen.findByTestId('inspector-body')
    await waitFor(() =>
      expect(body).toHaveTextContent('no typed absence is recorded'),
    )
    expect(body).not.toHaveTextContent('not measured for this desk')
  })

  // 7a/0222 — the contested-by-retrieval drill. The chip is one chip and says
  // one sentence; THIS is where the sentence has to be answerable for, and
  // until this lane the four fence columns the route serves stopped at the TS
  // boundary, so the drill asserted a contradiction and withheld its test.
  it('a contention drill shows the four fences, every page, and the bar', async () => {
    mockFetch()
    render(wrap(<InspectorPanel registration={reg()} scope={{}} mode="personal" />))
    useSelection.getState().select({
      kind: 'contention',
      id: 'c'.repeat(64),
      label: 'contested by retrieval',
      origin: 'claim-fold-chip',
    })

    const body = await screen.findByTestId('inspector-body')
    // F4 reads against its bar — "2" alone means nothing to a reader who does
    // not already know that a contradiction takes two independent outlets.
    await waitFor(() =>
      expect(body).toHaveTextContent('2 of 2 independent pages'),
    )
    expect(body).toHaveTextContent('the bar a contradiction needs')
    // The retrieval itself, and the rule that produced the stance.
    expect(body).toHaveTextContent('contradicts')
    expect(body).toHaveTextContent('polarity')
    expect(body).toHaveTextContent('hormuz reopened operating resumed')
    // BOTH pages, each with the host class and the date the gate parsed — the
    // encyclopedia entry included, with the fence that refused it, because
    // "one of these two could never have contradicted" is the whole point.
    expect(body).toHaveTextContent('https://news.example/hormuz-reopens')
    expect(body).toHaveTextContent('reporting')
    expect(body).toHaveTextContent('2026-09-20')
    expect(body).toHaveTextContent('https://en.wikipedia.org/wiki/Strait_of_Hormuz')
    expect(body).toHaveTextContent('reference')
    expect(body).toHaveTextContent('host_class')
    // And it still never adjudicates.
    expect(body).not.toHaveTextContent('the claim is false')
  })

  it('a contention written before the fences says NOT MEASURED, and expired', async () => {
    // A row from before migration 0222 measured none of the four. Printing 0
    // for any of them would say the pass looked and found nothing, which is a
    // different — and false — statement. An expired record is labelled rather
    // than hidden, for the same reason the chip labels it: a pass that stopped
    // running must not look like a week with nothing to contend.
    mockFetch(CONTENTION_UNFENCED)
    render(wrap(<InspectorPanel registration={reg()} scope={{}} mode="personal" />))
    useSelection.getState().select({
      kind: 'contention',
      id: 'c'.repeat(64),
      label: 'narrowed by retrieval',
      origin: 'claim-fold-chip',
    })

    const body = await screen.findByTestId('inspector-body')
    await waitFor(() => expect(body).toHaveTextContent('expired, not re-checked'))
    expect(body).toHaveTextContent('not measured')
    expect(body).toHaveTextContent('qualifies')
    // The fence lines that a zero would have falsified.
    expect(body).not.toHaveTextContent('0 of 2 independent pages')
    expect(body).not.toHaveTextContent("0 of the claim's subject words")
  })
})
