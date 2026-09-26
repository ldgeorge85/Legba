/**
 * MorningRead — the reading surface, rendered against the LIVE fixtures.
 *
 * These assertions are the design brief written as tests. Each one names the
 * ruling or the review finding it holds:
 *
 *   * Layout B default, one attribute, persisted            — ruling 3
 *   * three grains, each present and contexted               — ruling 5
 *   * the bounded question is the block header               — §B.4 Correction 3
 *   * attribution AFTER the prose, quiet                     — the design brief
 *   * tensions inline, and the checked negative deterministic — §1.6
 *   * the drop ledger honest but folded                      — §C.3.4 / §1.7
 *   * the Assessment a different voice, badged not shouting  — ruling 1 / §2.3
 *   * the voice a FUNCTION of the tier, never a constant      — Program 3 lane C
 *   * the evidence grain one drill-click down                — D-4 handoff
 *   * `brief_read` on mount; citation drills counted         — D2e / §5.3 G5
 *   * the history row names the read, not a truncated title  — VOICE §4.6.1
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

import MorningRead from './MorningRead'
import worldRow from './__fixtures__/assembly.world.json'
import countryRow from './__fixtures__/assembly.country.json'
import thematicRow from './__fixtures__/assembly.thematic.json'
import assessmentRow from './__fixtures__/assessment.world.json'
import countryAssessmentRow from './__fixtures__/assessment.country.json'
import { __resetReadLayout, getReadLayout } from '@/lib/readLayout'
import { __pendingReadEvents, __resetReadTelemetry } from '@/lib/readTelemetry'
import { projectAssembly, type ReadFindingRow } from '@/lib/assemblyModel'
import { scopeFromRow } from '@/lib/scopeFromReport'
import { resetScope, useScope } from '@/state/scope'

type Row = Record<string, unknown>

const WORLD = worldRow as unknown as Row
const COUNTRY = countryRow as unknown as Row
const THEMATIC = thematicRow as unknown as Row
const ASSESSMENT = assessmentRow as unknown as Row
const COUNTRY_ASSESSMENT = countryAssessmentRow as unknown as Row

/** Serve `/findings?analyst_id=…` from the fixtures, everything else empty. */
function stubFetch(byAnalyst: Record<string, Row[]>) {
  const fetchMock = vi.fn(async (url: string) => {
    const analyst = /analyst_id=([^&]+)/.exec(String(url))?.[1] ?? ''
    return {
      ok: true,
      status: 200,
      json: async () => ({ data: byAnalyst[analyst] ?? [] }),
      text: async () => '',
    } as unknown as Response
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

function kinds(): string[] {
  return __pendingReadEvents().map((e) => e.event_kind)
}

beforeEach(() => {
  __resetReadLayout()
  __resetReadTelemetry()
  // SCOPE decides which read is on the page; a scope left behind by one test
  // would silently choose the record for the next one.
  resetScope()
  sessionStorage.clear()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

async function renderWorld(extra: Record<string, Row[]> = {}) {
  stubFetch({ world_assessor: [WORLD], ...extra })
  render(wrap(<MorningRead />))
  await screen.findByTestId('morning-read')
}

// ---------------------------------------------------------------------------
// The three grains, and the ordering that carries them (rulings 3 and 5)
// ---------------------------------------------------------------------------

describe('the page', () => {
  it('renders all three grains, each labelled for what it is', async () => {
    await renderWorld()
    expect(screen.getByTestId('read-evidence-map')).toBeTruthy()
    expect(screen.getByTestId('read-record')).toBeTruthy()
    expect(screen.getByTestId('read-drop-ledger')).toBeTruthy()
    // The Assessment band is present even with no channel row — as an honest
    // absence, so the operator never wonders whether it silently vanished.
    expect(screen.getByTestId('read-assessment-absent')).toBeTruthy()
    // Each band names its own grain in its heading.
    const heading = (band: string) =>
      within(screen.getByTestId(band)).getByRole('heading', { level: 2 }).textContent
    expect(heading('read-evidence-map')).toMatch(/Signals & correlation/)
    expect(heading('read-record')).toMatch(/The record/)
    expect(heading('read-drop-ledger')).toMatch(/Not in this read/)
    expect(heading('read-assessment-absent')).toMatch(/The Assessment/)
  })

  it('defaults to Layout B and flips on ONE attribute', async () => {
    await renderWorld()
    const page = screen.getByTestId('read-page')
    expect(page.getAttribute('data-read-layout')).toBe('B')
    expect(screen.getByTestId('read-layout-B').getAttribute('aria-pressed')).toBe('true')

    fireEvent.click(screen.getByTestId('read-layout-A'))
    await waitFor(() =>
      expect(screen.getByTestId('read-page').getAttribute('data-read-layout')).toBe('A'),
    )
    // Persisted, so a Dockview remount or a reload keeps the operator's order.
    expect(getReadLayout()).toBe('A')
    // Same nodes, reordered by CSS — no band was unmounted by the flip.
    expect(screen.getByTestId('read-record')).toBeTruthy()
    expect(screen.getByTestId('read-evidence-map')).toBeTruthy()
  })

  it('opens on the world, keeping the as-of stamp as metadata (VOICE A4)', async () => {
    await renderWorld()
    const title = screen.getByTestId('read-title').textContent ?? ''
    expect(title.length).toBeGreaterThan(0)
    // The stamp is in the kicker above the headline, not in the headline.
    expect(screen.getByTestId('read-kicker').textContent).toMatch(/UTC/)
    expect(title).not.toMatch(/UTC/)
    expect(screen.getByTestId('read-standfirst').textContent).toMatch(/quoted verbatim/)
  })
})

// ---------------------------------------------------------------------------
// The record: quoted prose, quiet furniture, the bounded question as header
// ---------------------------------------------------------------------------

describe('the record band', () => {
  it('renders one block per carried head, headed by the bounded QUESTION', async () => {
    await renderWorld()
    const blocks = screen.getAllByTestId('read-block')
    expect(blocks).toHaveLength(8)
    const first = within(blocks[0])
    const question = first.getByTestId('read-question').textContent ?? ''
    expect(question.length).toBeGreaterThan(10)
    // A question, not a headline: it ends in a question mark or names the desk.
    expect(question).toMatch(/\?|[a-z]/)
  })

  it('quotes the desk sentence VERBATIM from the payload span', async () => {
    await renderWorld()
    const a = projectAssembly(WORLD as unknown as ReadFindingRow)!
    const lede = screen.getAllByTestId('read-lede')[0].textContent ?? ''
    const spanText = a.blocks[0].spans[0].text.replace(/\s*\[\d+\]/g, '').trim()
    // Citation markers become chips, so compare the prose either side of them.
    expect(lede.replace(/\s+/g, ' ')).toContain(spanText.slice(0, 40))
  })

  it('puts attribution AFTER the prose and keeps the verify score in the rail', async () => {
    await renderWorld()
    const block = screen.getAllByTestId('read-block')[0]
    const lede = within(block).getByTestId('read-lede')
    const attrib = within(block).getByTestId('read-attrib')
    // DOM order is reading order: the sentence, then who said it.
    expect(lede.compareDocumentPosition(attrib) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(attrib.textContent).toMatch(/verbatim from head/)
    // The verify number is a rail element, not a chip in the reader's path.
    const verify = within(block).getByTestId('read-block-verify')
    expect(verify.closest('.read-rail')).not.toBeNull()
  })

  it('states the concentration verdict as the deterministic reason it is', async () => {
    await renderWorld()
    const reason = screen.getByTestId('read-lead-reason').textContent ?? ''
    expect(reason).toMatch(/top-share/)
    expect(reason).toMatch(/bars/)
  })

  it('carries no faithfulness score on the record badge', async () => {
    await renderWorld()
    const badge = screen.getByTestId('read-record-badge').textContent ?? ''
    expect(badge).toMatch(/Quote fidelity/)
    expect(badge).toMatch(/dropped/)
    expect(badge).toMatch(/is not a badge/)
  })
})

// ---------------------------------------------------------------------------
// Tensions, inline
// ---------------------------------------------------------------------------

describe('tensions', () => {
  it('renders a declared tension INSIDE the record, after its later block', async () => {
    // The fixture is a country read; point the surface at its producer.
    stubFetch({ country_composition: [COUNTRY] })
    render(wrap(<MorningRead analystId="country_composition" />))
    await screen.findByTestId('morning-read')
    const notes = await screen.findAllByTestId('read-tension')
    expect(notes.length).toBeGreaterThan(0)
    const record = screen.getByTestId('read-record')
    expect(record.contains(notes[0])).toBe(true)
    expect(notes[0].textContent).toMatch(/Declared, not explained/)
  })

  it('names the dropped half of a carried_vs_dropped tension', async () => {
    await renderWorld()
    const cvd = screen
      .getAllByTestId('read-tension')
      .find((n) => n.getAttribute('data-kind') === 'carried_vs_dropped')
    expect(cvd).toBeDefined()
    expect(cvd!.textContent).toMatch(/did not reach this read/)
    expect(cvd!.textContent).toMatch(/named in the ledger below/)
  })

  it('renders the CHECKED NEGATIVE with its counters and its scope', async () => {
    // The thematic read found no conflicting pair — the branch that must not
    // become "all verified reads are mutually consistent" again.
    stubFetch({ escalation_composition: [THEMATIC] })
    render(wrap(<MorningRead analystId="escalation_composition" />))
    await screen.findByTestId('morning-read')
    const neg = await screen.findByTestId('read-tension-negative')
    expect(neg.textContent).toMatch(/pairs examined/)
    expect(neg.textContent).toMatch(/ambivalent pairs declined/)
    expect(neg.textContent).toMatch(/neither manufacture a tension nor dissolve one/)
  })
})

// ---------------------------------------------------------------------------
// The evidence grain: one drill-click down
// ---------------------------------------------------------------------------

describe('the evidence grain', () => {
  it('shows the correlation overview, not a wall of signals', async () => {
    await renderWorld()
    const map = screen.getByTestId('read-evidence-map')
    expect(within(map).getByTestId('read-evidence-summary').textContent).toMatch(/wire item/)
    // No per-block signal list is rendered until a block is drilled.
    expect(screen.queryAllByTestId('read-signal')).toHaveLength(0)
  })

  it('opens ONE block’s evidence on the drill and counts it as a lineage walk', async () => {
    await renderWorld()
    const block = screen.getAllByTestId('read-block')[0]
    fireEvent.click(within(block).getByTestId('read-evidence-toggle'))
    await waitFor(() => expect(within(block).getByTestId('read-block-evidence')).toBeTruthy())
    expect(within(block).getAllByTestId('read-signal').length).toBeGreaterThan(0)
    // Only the drilled block opened.
    const otherBlocks = screen.getAllByTestId('read-block').slice(1)
    for (const b of otherBlocks) {
      expect(within(b).queryByTestId('read-block-evidence')).toBeNull()
    }
    expect(kinds()).toContain('lineage_walk')
  })

  it('mounts the bounded unit reads beneath the drill (VOICE §4.6.2)', async () => {
    await renderWorld()
    const block = screen.getAllByTestId('read-block')[0]
    fireEvent.click(within(block).getByTestId('read-evidence-toggle'))
    await waitFor(() => expect(within(block).getByTestId('read-block-units')).toBeTruthy())
  })
})

// ---------------------------------------------------------------------------
// The drop ledger
// ---------------------------------------------------------------------------

describe('the drop ledger', () => {
  it('states itself in one sentence and folds the rows', async () => {
    await renderWorld()
    const ledger = screen.getByTestId('read-drop-ledger')
    expect(within(ledger).getByTestId('read-drop-summary').textContent).toMatch(/below the line/)
    expect(within(ledger).queryByTestId('read-drop-detail')).toBeNull()
    fireEvent.click(within(ledger).getByTestId('read-drop-toggle'))
    await waitFor(() => expect(within(ledger).getByTestId('read-drop-detail')).toBeTruthy())
    expect(within(ledger).getAllByTestId('read-drop-row').length).toBeGreaterThan(0)
  })

  it('publishes invisible_heads as a COUNT and says why it is not a list', async () => {
    await renderWorld()
    const note = screen.getByTestId('read-invisible-heads').textContent ?? ''
    expect(note).toMatch(/published as a count and not as a list/)
    expect(note).toMatch(/nothing records a reason/)
  })

  it('renders coverage from the persisted ledger, not from prose', async () => {
    await renderWorld()
    expect(screen.getByTestId('read-coverage').textContent).toMatch(
      /from the persisted ledger and not from prose/,
    )
  })
})

// ---------------------------------------------------------------------------
// The Assessment channel
// ---------------------------------------------------------------------------

describe('the Assessment band', () => {
  async function renderWithChannel() {
    stubFetch({ world_assessor: [WORLD], world_assessment: [ASSESSMENT] })
    render(wrap(<MorningRead />))
    await screen.findByTestId('read-assessment')
  }

  it('is set in a different VOICE, labelled and badged without shouting', async () => {
    await renderWithChannel()
    const band = screen.getByTestId('read-assessment')
    expect(within(band).getByTestId('read-assessment-label').textContent).toMatch(
      /not the record/,
    )
    const badge = within(band).getByTestId('read-assessment-badge').textContent ?? ''
    // Every number carries its population; none is bare.
    expect(badge).toMatch(/0\.4805/)
    expect(badge).toMatch(/n=77/)
    expect(badge).toMatch(/PRE-assembly prose tier/)
    // Pre-D-3 there is no fidelity-to-spine number. It is stated as unmeasured,
    // never rendered as 0.00 — which would read as a measured failure.
    expect(badge).toMatch(/not measured yet/)
    expect(badge).not.toMatch(/0\.00/)
  })

  it('marks the unsupported-by-spine spans inline, with their notes', async () => {
    await renderWithChannel()
    const marks = screen.getAllByTestId('read-unsupported')
    expect(marks.length).toBe(2)
    expect(marks.map((m) => m.getAttribute('data-class')).sort()).toEqual(['fact', 'rank'])
    const notes = screen.getByTestId('read-unsupported-notes').textContent ?? ''
    expect(notes).toMatch(/RANK/)
    expect(notes).toMatch(/FACT/)
    // The prose survives the marking — the marked words are still in the body.
    expect(screen.getByTestId('read-assessment-body').textContent).toMatch(
      /defines the top risk in this cycle/,
    )
  })

  it('shows the live fidelity number, with its own population, once D-3 lands', async () => {
    const graded = JSON.parse(JSON.stringify(ASSESSMENT)) as Record<string, unknown>
    const payload = (
      ((graded.data as Record<string, unknown>).data as Record<string, unknown>)
        .assessment as Record<string, unknown>
    )
    payload.fidelity = { checkable: 4, supported: 4, score: 1.0 }
    ;(payload.badge as Record<string, unknown>).fidelity_to_spine = 1.0
    stubFetch({ world_assessor: [WORLD], world_assessment: [graded as Row] })
    render(wrap(<MorningRead />))
    await screen.findByTestId('read-assessment')
    const badge = screen.getByTestId('read-assessment-badge').textContent ?? ''
    expect(badge).toMatch(/Fidelity to the spine/)
    expect(badge).toMatch(/1\.00/)
    // It never pools with the record's 1.0-by-construction number.
    expect(badge).toMatch(/its own population/)
  })

  it('hands the concentration verdict to the voice as an arithmetic fact', async () => {
    await renderWithChannel()
    expect(screen.getByTestId('read-assessment-leadtest').textContent).toMatch(
      /handed to this voice as a fact/,
    )
  })

  it('is LAST in the default order and FIRST when flipped', async () => {
    await renderWithChannel()
    const page = screen.getByTestId('read-page')
    expect(page.getAttribute('data-read-layout')).toBe('B')
    expect(screen.getByTestId('read-assessment').getAttribute('data-band')).toBe('assessment')
    fireEvent.click(screen.getByTestId('read-layout-A'))
    await waitFor(() =>
      expect(screen.getByTestId('read-page').getAttribute('data-read-layout')).toBe('A'),
    )
    // The band did not move in the DOM — only its CSS order changed, which is
    // what makes the flip cheap.
    expect(screen.getByTestId('read-assessment')).toBeTruthy()
  })
})

// ---------------------------------------------------------------------------
// The country channel: the same band, fenced to the country record (lane C)
// ---------------------------------------------------------------------------

describe('the country Assessment band', () => {
  /** The Taiwan desk read, with whatever the channel is serving that cycle. */
  async function renderCountry(extra: Record<string, Row[]> = {}) {
    const fetchMock = stubFetch({ country_composition: [COUNTRY], ...extra })
    render(wrap(<MorningRead analystId="country_composition" />))
    await screen.findByTestId('morning-read')
    return fetchMock
  }

  function urls(fetchMock: ReturnType<typeof stubFetch>): string[] {
    return fetchMock.mock.calls.map((c) => String(c[0]))
  }

  it('renders the country voice under the country record, same band, same look', async () => {
    await renderCountry({ country_assessment: [COUNTRY_ASSESSMENT] })
    const band = await screen.findByTestId('read-assessment')
    // The heading is the one the operator learned on the world read…
    expect(within(band).getByRole('heading', { level: 2 }).textContent).toMatch(/The Assessment/)
    // …and the label line says WHICH voice this is.
    expect(within(band).getByTestId('read-assessment-label').textContent).toMatch(
      /Country assessment · written from the spine above/,
    )
    expect(within(band).getByTestId('read-assessment-label').textContent).toMatch(/not the record/)
    expect(within(band).getByTestId('read-assessment-title').textContent).toMatch(
      /Taiwan holds at elevated/,
    )
    expect(within(band).getByTestId('read-assessment-body').textContent).toMatch(
      /patrol cadence/,
    )
    // Fenced to THIS record: no mismatch note, and no stray-ordinal note.
    expect(within(band).queryByTestId('read-assessment-spine-mismatch')).toBeNull()
    expect(within(band).queryByTestId('read-assessment-stray')).toBeNull()
    // The machinery the world band already has works unchanged at country grain.
    expect(within(band).getAllByTestId('read-unsupported').map((m) => m.getAttribute('data-class')))
      .toEqual(['rank', 'fact'])
    expect(within(band).getByTestId('read-assessment-leadtest').textContent).toMatch(
      /handed to this voice as a fact/,
    )
  })

  it('asks the read path for the country channel AND the desk, never the world voice', async () => {
    const fetchMock = await renderCountry({ country_assessment: [COUNTRY_ASSESSMENT] })
    await screen.findByTestId('read-assessment')
    const asked = urls(fetchMock)
    expect(asked.some((u) => /analyst_id=country_assessment/.test(u))).toBe(true)
    expect(asked.some((u) => /analyst_id=country_assessment.*target_id=country_watch_tw/.test(u)))
      .toBe(true)
    expect(asked.some((u) => /analyst_id=world_assessment/.test(u))).toBe(false)
  })

  it('states a voice written from ANOTHER run of this desk as exactly that', async () => {
    const stale = JSON.parse(JSON.stringify(COUNTRY_ASSESSMENT)) as Record<string, unknown>
    stale.id = 'stale-country-voice'
    stale.derived_from = ['a-previous-taiwan-run']
    ;(
      ((stale.data as Record<string, unknown>).data as Record<string, unknown>)
        .assessment as Record<string, unknown>
    ).spine_id = 'a-previous-taiwan-run'
    await renderCountry({ country_assessment: [stale as Row] })
    const band = await screen.findByTestId('read-assessment')
    expect(within(band).getByTestId('read-assessment-spine-mismatch').textContent).toMatch(
      /written from a different run of the record/,
    )
  })

  it('reads the day-one absence honestly — the analyst is in draft, not broken', async () => {
    await renderCountry()
    const absent = await screen.findByTestId('read-assessment-absent')
    const copy = absent.textContent ?? ''
    expect(copy).toMatch(/No country assessment has been written from this record yet/)
    expect(copy).toMatch(/starts in draft/)
    expect(copy).toMatch(/normal state and not a failure/)
    // The record is whole without it, and nothing on the page is an error.
    expect(screen.getByTestId('read-record')).toBeTruthy()
    expect(screen.queryByTestId('read-error')).toBeNull()
  })

  it('never borrows the WORLD voice, nor a neighbour desk’s', async () => {
    const neighbour = JSON.parse(JSON.stringify(COUNTRY_ASSESSMENT)) as Record<string, unknown>
    neighbour.id = 'israel-voice'
    neighbour.target_id = 'country_watch_il'
    neighbour.derived_from = ['an-israel-run']
    await renderCountry({
      world_assessment: [ASSESSMENT],
      country_assessment: [neighbour as Row],
    })
    expect(await screen.findByTestId('read-assessment-absent')).toBeTruthy()
    expect(screen.queryByTestId('read-assessment')).toBeNull()
  })

  it('follows a country SCOPE to that desk’s record, and to that desk’s voice', async () => {
    const fetchMock = stubFetch({
      country_composition: [COUNTRY],
      country_assessment: [COUNTRY_ASSESSMENT],
    })
    useScope.getState().setScope(scopeFromRow(COUNTRY as unknown as ReadFindingRow))
    render(wrap(<MorningRead />)) // mounted on the WORLD spine; the scope wins
    await screen.findByTestId('morning-read')
    expect(screen.getByTestId('read-kicker').textContent).toMatch(/country read/)
    expect((await screen.findByTestId('read-assessment-title')).textContent).toMatch(
      /Taiwan holds at elevated/,
    )
    // The spine query is narrowed to the scoped desk: `country_composition`
    // publishes one row per desk per cycle and RUN_LIMIT is 12, so an unnarrowed
    // query need not contain the desk the operator asked for.
    expect(
      urls(fetchMock).some((u) =>
        /analyst_id=country_composition&target_id=country_watch_tw/.test(u),
      ),
    ).toBe(true)
  })

  it('scoping to the VOICE reads the record it was written from', async () => {
    stubFetch({ country_composition: [COUNTRY], country_assessment: [COUNTRY_ASSESSMENT] })
    useScope.getState().setScope(scopeFromRow(COUNTRY_ASSESSMENT as unknown as ReadFindingRow))
    render(wrap(<MorningRead />))
    await screen.findByTestId('morning-read')
    // The page is the country ASSEMBLY, never the voice's own row rendered as a
    // record — with the voice back in its band underneath.
    expect(screen.getByTestId('read-kicker').textContent).toMatch(/country read/)
    expect(screen.getAllByTestId('read-block').length).toBe(8)
    expect((await screen.findByTestId('read-assessment-title')).textContent).toMatch(
      /Taiwan holds at elevated/,
    )
  })

  it('renders NO band at a tier that has no channel, rather than an empty one', async () => {
    // A thematic read has no interpretive voice. Before the channel became a
    // function of the tier, the world assessment fell through to here.
    stubFetch({ escalation_composition: [THEMATIC], world_assessment: [ASSESSMENT] })
    render(wrap(<MorningRead analystId="escalation_composition" />))
    await screen.findByTestId('morning-read')
    expect(screen.queryByTestId('read-assessment')).toBeNull()
    expect(screen.queryByTestId('read-assessment-absent')).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// The gate, the legacy path, the history list, and telemetry
// ---------------------------------------------------------------------------

describe('the verify gate at READ time (§3.6)', () => {
  it('withholds the failed block instead of showing it with a caveat', async () => {
    const failed = JSON.parse(JSON.stringify(WORLD)) as Record<string, unknown>
    const verification = failed.verification as Record<string, unknown>
    verification.assembly_arms = {
      ...(verification.assembly_arms as Record<string, unknown>),
      gate: 'failed',
      failed_ordinals: [3],
    }
    stubFetch({ world_assessor: [failed] })
    render(wrap(<MorningRead />))
    await screen.findByTestId('morning-read')
    expect(screen.getByTestId('read-gate-failed').textContent).toMatch(/construction regression/)
    const refused = screen.getAllByTestId('read-block-refused')
    expect(refused).toHaveLength(1)
    expect(refused[0].textContent).toMatch(/Block 3 is withheld/)
    // Seven blocks still read; only the unprovable one is withheld.
    expect(screen.getAllByTestId('read-lede')).toHaveLength(7)
  })
})

describe('a run that predates the assembly', () => {
  it('is shown as composed prose and SAYS SO, never as an error', async () => {
    stubFetch({
      world_assessor: [
        {
          id: 'legacy-1',
          analyst_id: 'world_assessor',
          produced_at: '2026-09-01T12:00:00Z',
          title: 'A composed world read',
          body: '**BLUF:** the old shape.',
          data: { data: { meta: true, citations: [] } },
        },
      ],
    })
    render(wrap(<MorningRead />))
    await screen.findByTestId('read-legacy')
    expect(screen.getByTestId('read-legacy').textContent).toMatch(/predates the assembly/)
    expect(screen.queryByTestId('read-error')).toBeNull()
  })
})

describe('the history list (VOICE §4.6.1)', () => {
  it('labels a prior run by lead, block count and drop count', async () => {
    const older = JSON.parse(JSON.stringify(WORLD)) as Record<string, unknown>
    older.id = 'older-run'
    older.produced_at = '2026-09-02T12:00:00Z'
    stubFetch({ world_assessor: [WORLD, older] })
    render(wrap(<MorningRead />))
    await screen.findByTestId('read-history')
    const row = screen.getByTestId('read-history-row').textContent ?? ''
    expect(row).toMatch(/blocks/)
    expect(row).toMatch(/dropped/)
    // Not a truncated repetition of the same sentence 60 times.
    expect(row).not.toBe(String(WORLD.title))
  })
})

describe('read telemetry (D2e)', () => {
  it('fires brief_read once when the brief actually renders', async () => {
    await renderWorld()
    await waitFor(() => expect(kinds()).toContain('brief_read'))
    expect(kinds().filter((k) => k === 'brief_read')).toHaveLength(1)
  })

  it('does not fire brief_read when there is no read to render', async () => {
    stubFetch({ world_assessor: [] })
    render(wrap(<MorningRead />))
    await screen.findByTestId('read-empty')
    expect(kinds()).not.toContain('brief_read')
  })

  it('counts a citation drill from a chip inside a quoted span', async () => {
    await renderWorld()
    const chip = document.querySelector('[data-testid="morning-read"] button[title*="["]')
    // The chips are `CitedProse`'s, so find one by its rendered marker label.
    const markers = screen.getAllByRole('button').filter((b) => /^\[?\d+\]?$/.test(b.textContent ?? ''))
    const target = (chip as HTMLElement | null) ?? markers[0]
    expect(target).toBeTruthy()
    fireEvent.click(target)
    await waitFor(() => expect(kinds()).toContain('citation_drill'))
  })

  it('records a walk to an origin head as finding_open', async () => {
    await renderWorld()
    fireEvent.click(screen.getAllByTestId('read-open-head')[0])
    await waitFor(() => expect(kinds()).toContain('finding_open'))
  })
})
