/**
 * assemblyModel — the projector and the reader's derived facts.
 *
 * The fixtures are REAL: three `assembly.v1` payloads built from the live
 * 2026-09-03 cycle by read-only SELECT (32 country assemblies for the world
 * spine per §4.2, one country read, one thematic read), plus the live
 * `world_assessor` prose as the Assessment channel's specimen with the two
 * unsupported classes the spec names. They are checked in so this suite tests
 * the shapes the producer will actually emit, not shapes invented to pass.
 */
import { describe, it, expect } from 'vitest'
import worldRow from '@/v4/read/__fixtures__/assembly.world.json'
import countryRow from '@/v4/read/__fixtures__/assembly.country.json'
import thematicRow from '@/v4/read/__fixtures__/assembly.thematic.json'
import assessmentRow from '@/v4/read/__fixtures__/assessment.world.json'
import citationRow from '@/v4/read/__fixtures__/assessment.citations.json'
import {
  blockRefused,
  projectStanding,
  standingSentence,
  evidenceTotals,
  historyLabel,
  leadReason,
  projectArms,
  projectAssembly,
  projectAssessment,
  projectAssessmentCitations,
  recordBadge,
  resolveAssessmentCitations,
  sharedSignals,
  tensionsByAnchor,
  type ReadFindingRow,
} from './assemblyModel'

const world = worldRow as unknown as ReadFindingRow
const country = countryRow as unknown as ReadFindingRow
const thematic = thematicRow as unknown as ReadFindingRow
const assessment = assessmentRow as unknown as ReadFindingRow
const assessmentCitations = citationRow as unknown as ReadFindingRow

describe('projectAssembly', () => {
  it('projects all three live tiers', () => {
    for (const [name, row] of [
      ['world', world],
      ['country', country],
      ['thematic', thematic],
    ] as const) {
      const a = projectAssembly(row)
      expect(a, name).not.toBeNull()
      expect(a!.schema).toBe('assembly.v1')
      expect(a!.regime).toBe('assembly')
      expect(a!.blocks.length).toBeGreaterThan(0)
    }
  })

  it('keeps ordinals dense and 1-based, so [[ref:N]] indexes the page', () => {
    const a = projectAssembly(world)!
    expect(a.blocks.map((b) => b.ordinal)).toEqual([1, 2, 3, 4, 5, 6, 7, 8])
  })

  it('carries every span with a DEPTH-1 origin equal to the block head', () => {
    const a = projectAssembly(world)!
    for (const b of a.blocks) {
      expect(b.spans.length).toBeGreaterThan(0)
      for (const s of b.spans) {
        // §1.2 — origin.head_id is ALWAYS the block's own head. Never a path.
        expect(s.origin.head_id).toBe(b.finding_id)
        expect(s.origin.body_sha256).toMatch(/^[0-9a-f]{64}$/)
        expect(s.origin.end).toBeGreaterThan(s.origin.start)
        expect(s.origin.end).toBeLessThanOrEqual(s.origin.body_len)
      }
    }
  })

  it('gives every block a bounded question, stamping the fallback', () => {
    const a = projectAssembly(country)!
    for (const b of a.blocks) {
      expect(b.question.length).toBeGreaterThan(0)
      expect(['descriptor', 'fallback_desk_name']).toContain(b.question_source)
    }
  })

  it('returns null for a legacy prose run rather than throwing', () => {
    const legacy: ReadFindingRow = {
      id: 'legacy',
      produced_at: '2026-09-01T12:00:00Z',
      analyst_id: 'world_assessor',
      body: '**BLUF:** something happened.',
      data: { data: { meta: true, citations: [] } },
    }
    expect(projectAssembly(legacy)).toBeNull()
  })

  it('refuses a future schema instead of half-rendering it', () => {
    const future: ReadFindingRow = {
      id: 'future',
      produced_at: '2026-09-01T12:00:00Z',
      data: { data: { assembly: { schema: 'assembly.v2', blocks: [] } } },
    }
    expect(projectAssembly(future)).toBeNull()
  })

  it('survives a payload delivered as a JSON string', () => {
    const stringy: ReadFindingRow = {
      id: 'stringy',
      produced_at: '2026-09-03T12:00:00Z',
      data: JSON.stringify((world.data as Record<string, unknown>) ?? {}),
    }
    expect(projectAssembly(stringy)?.blocks.length).toBe(8)
  })
})

describe('the drop ledger identity (§1.3)', () => {
  it('|blocks| == counts.carried on every tier', () => {
    for (const row of [world, country, thematic]) {
      const a = projectAssembly(row)!
      expect(a.drops!.counts.carried).toBe(a.blocks.length)
    }
  })

  it('publishes a why-class from the closed enum on every dropped row', () => {
    const a = projectAssembly(thematic)!
    const enumSet = new Set(a.drops!.why_classes)
    expect(enumSet.size).toBeGreaterThan(0)
    const rows = [
      ...a.drops!.shown_not_carried,
      ...a.drops!.not_selected,
      ...a.drops!.trimmed,
      ...a.drops!.below_floor,
    ]
    expect(rows.length).toBeGreaterThan(0)
    for (const r of rows) expect(enumSet.has(r.why)).toBe(true)
  })

  it('keeps invisible_heads a COUNT and never a list', () => {
    const a = projectAssembly(world)!
    expect(a.drops!.counts.invisible_heads).toBeGreaterThan(0)
    // Nothing anywhere in the ledger enumerates them: no why-class is derivable.
    const listed = [
      ...a.drops!.shown_not_carried,
      ...a.drops!.not_selected,
      ...a.drops!.trimmed,
      ...a.drops!.below_floor,
    ].length
    expect(listed).toBeLessThan(a.drops!.counts.invisible_heads)
  })

  it('honours the strict-prefix property: no carried block outranks a drop', () => {
    const a = projectAssembly(world)!
    const worstCarried = a.blocks.length
    for (const r of a.drops!.not_selected) {
      expect(r.rank).not.toBeNull()
      expect(r.rank!).toBeGreaterThan(worstCarried)
    }
  })
})

describe('the earned-lead test (§1.5.3)', () => {
  it('stamps the reason whichever branch fired', () => {
    for (const row of [world, country, thematic]) {
      const a = projectAssembly(row)!
      expect(a.lead?.test).not.toBeNull()
      expect(leadReason(a.lead)).toMatch(/top-share/)
    }
  })

  it('agrees with its own arithmetic: earned iff both bars are cleared', () => {
    for (const row of [world, country, thematic]) {
      const t = projectAssembly(row)!.lead!.test!
      expect(t.earned).toBe(
        t.ratio_12 >= t.bar_ratio && t.top_share >= t.bar_share && t.n_candidates >= 8,
      )
    }
  })

  it('names co-leads without crowning one', () => {
    const a = projectAssembly(thematic)!
    expect(a.lead!.kind).toBe('co_leads')
    expect(a.lead!.block_ordinals.length).toBeGreaterThanOrEqual(2)
    expect(leadReason(a.lead)).toContain('none crowned')
  })

  it('reads a flat day as an explicit no-lead state', () => {
    expect(
      leadReason({
        kind: 'none',
        block_ordinals: [],
        test: {
          key: 'cited_mass.v1',
          top_share: 0.086,
          ratio_12: 1.03,
          bar_share: 0.15,
          bar_ratio: 1.5,
          earned: false,
          n_candidates: 32,
        },
      }),
    ).toContain('no single lead this cycle')
  })
})

describe('tensions', () => {
  it('anchors each tension after the LATER of its two blocks', () => {
    const a = projectAssembly(country)!
    const anchored = tensionsByAnchor(a)
    expect(anchored.size).toBeGreaterThan(0)
    for (const [anchor, list] of anchored) {
      for (const t of list) {
        const ords = [t.a?.ordinal, t.b?.ordinal].filter((o): o is number => typeof o === 'number')
        expect(Math.max(...ords.filter((o) => o <= a.blocks.length))).toBe(anchor)
      }
    }
  })

  it('anchors a carried_vs_dropped on its ONE carried half', () => {
    const a = projectAssembly(world)!
    const cvd = a.tensions.find((t) => t.kind === 'carried_vs_dropped')
    expect(cvd).toBeDefined()
    expect(cvd!.b).toBeNull()
    expect(cvd!.b_ref).not.toBeNull()
    expect(tensionsByAnchor(a).get(cvd!.a!.ordinal)).toContain(cvd)
  })

  it('publishes the checked negative WITH its scope, so it is not M-11 again', () => {
    const a = projectAssembly(thematic)!
    expect(a.tension_checked!.pairs_found).toBe(0)
    expect(a.tension_checked!.pairs_examined).toBeGreaterThan(0)
    expect(a.tension_checked!.scope_note).toContain('ambivalent pairs declined')
  })

  // W-1 — a carried head paired with one the record did NOT carry is evidence
  // for the drop ledger, not a conflict the record published. The reader must
  // be able to tell the two apart WITHOUT parsing the statement.
  it('marks a carried_vs_dropped tension as having an uncarried B side', () => {
    const a = projectAssembly(world)!
    const cvd = a.tensions.find((t) => t.kind === 'carried_vs_dropped')!
    expect(cvd.b_carried).toBe(false)
    const pair = a.tensions.find((t) => t.kind === 'carried_pair')
    if (pair) expect(pair.b_carried).toBe(true)
  })

  it('derives b_carried from the shape when the producer stamped no field', () => {
    // The world fixture predates the W-1 stamp, which is exactly the case that
    // must not default to `true`: `carried_vs_dropped` with a `b_ref` and no
    // `b` ordinal WAS an uncarried B side all along, and the projection says so
    // rather than inventing a carried one.
    const raw = projectAssembly(world)!.tensions[0]
    expect(raw.b).toBeNull()
    expect(raw.b_ref).not.toBeNull()
    expect(raw.b_carried).toBe(false)
  })

  it('reports an unmeasured split as null, never as zero', () => {
    // A record written before W-1 published no split. Zero here would read as
    // "checked, and none were cross-tier" — the opposite of what happened.
    const a = projectAssembly(world)!
    expect(a.tension_checked!.pairs_found_carried).toBeNull()
    expect(a.tension_checked!.pairs_found_uncarried).toBeNull()
  })
})

describe('the record badge (§2.3)', () => {
  it('carries quote fidelity, coverage and a drop count — and NO faithfulness', () => {
    const a = projectAssembly(world)!
    const badge = recordBadge(a, projectArms(world))
    expect(badge.quoteFidelity).toBe(1)
    expect(badge.coverageCompleteness).toBe(1)
    expect(badge.blocks).toBe(8)
    expect(badge.dropCount).toBeGreaterThan(0)
    expect(badge.gate).toBe('passed')
    expect(Object.keys(badge)).not.toContain('faithfulness')
  })

  it('reports an unmeasured gate as unmeasured, never as passed', () => {
    const a = projectAssembly(world)!
    expect(recordBadge(a, null).gate).toBe('unmeasured')
  })

  it('refuses only the ordinals the failed arm named', () => {
    const arms = { ...projectArms(world)!, gate: 'failed', failed_ordinals: [3] }
    expect(blockRefused(arms, 3)).toBe(true)
    expect(blockRefused(arms, 1)).toBe(false)
    expect(blockRefused(null, 3)).toBe(false)
  })
})

describe('the evidence map grain', () => {
  it('counts signals, sources and the scored fraction off the blocks', () => {
    const t = evidenceTotals(projectAssembly(world)!)
    expect(t.signals).toBeGreaterThan(0)
    expect(t.distinctSignals).toBeGreaterThan(0)
    expect(t.sources).toBeGreaterThan(0)
    expect(t.scored).toBeLessThanOrEqual(t.signals)
  })

  it('surfaces only signals cited by more than one desk head', () => {
    const a = projectAssembly(world)!
    for (const s of sharedSignals(a)) {
      expect(s.citedByOrdinals.length + s.sharedWith.length).toBeGreaterThan(1)
    }
  })
})

describe('the history row (VOICE §4.6.1)', () => {
  it('names the lead, the block count and the drop count — not a truncated title', () => {
    const label = historyLabel(projectAssembly(world))!
    expect(label.blocks).toBe(8)
    expect(label.dropped).toBeGreaterThan(0)
    expect(label.lead).toMatch(/\//) // "<desk> / <target>"
  })

  it('says so plainly when there is no lead', () => {
    expect(
      historyLabel({
        schema: 'assembly.v1',
        regime: 'assembly',
        tier: 'world',
        as_of: '2026-09-03T12:00:00Z',
        lead: { kind: 'none', block_ordinals: [], test: null },
        blocks: [],
        tensions: [],
        tension_checked: null,
        drops: null,
        coverage: [],
        connectives: null,
      })!.lead,
    ).toBe('no single lead')
  })

  it('returns null for a pre-assembly run so the row keeps its title', () => {
    expect(historyLabel(null)).toBeNull()
  })
})

describe('the Assessment channel (§2)', () => {
  it('projects the payload and its single-element derived_from', () => {
    const p = projectAssessment(assessment)!
    expect(p.schema).toBe('assessment.v1')
    expect(assessment.derived_from).toHaveLength(1)
    expect(assessment.derived_from![0]).toBe(p.spine_id)
    expect(p.spine_schema).toBe('assembly.v1')
  })

  it('resolves every [[ref:N]] marker INTO the spine', () => {
    const p = projectAssessment(assessment)!
    const spine = projectAssembly(world)!
    const ordinals = new Set(spine.blocks.map((b) => b.ordinal))
    expect(p.markers.length).toBeGreaterThan(0)
    for (const m of p.markers) expect(ordinals.has(m.ordinal)).toBe(true)
  })

  it('carries both detector families of unsupported marker, with offsets', () => {
    const p = projectAssessment(assessment)!
    const classes = p.unsupported.map((u) => u.class)
    expect(classes).toContain('rank')
    expect(classes).toContain('fact')
    for (const u of p.unsupported) {
      // The offsets must actually name the words they claim to name.
      expect(assessment.body!.slice(u.char_start, u.char_end)).toBe(u.text)
      expect(u.note.length).toBeGreaterThan(0)
    }
  })

  it('never shows a bare external number: the population rides with it', () => {
    const badge = projectAssessment(assessment)!.badge!
    expect(badge.external_accuracy!.population).not.toBe('')
    expect(badge.external_accuracy!.n).toBeGreaterThan(0)
    expect(badge.external_accuracy_note).toContain('PRE-assembly')
    // Until D-3 lands there is no live fidelity number, and null is the honest
    // value — never 0, which would read as a measured failure.
    expect(badge.fidelity_to_spine).toBeNull()
  })

  it('returns null for a row that is not an Assessment', () => {
    expect(projectAssessment(world)).toBeNull()
  })
})

// ── W-7 · the standing badge, on the reader's side ───────────────────────────

describe('the standing number (§3.4)', () => {
  const block = (over: Record<string, unknown> = {}) =>
    projectStanding({
      value: 0.7125,
      state: 'measured',
      n_decided: 1148,
      n_searched: 3290,
      decided_rate: 0.349,
      window_days: 7,
      population: 'assembly_span',
      grader_family: 'gemma',
      instrument_limited: false,
      sample_fraction: null,
      state_note: null,
      ...over,
    })!

  it('never renders a bare number: the n and the window always travel with it', () => {
    const line = standingSentence(block(), 'The record')
    expect(line).toContain('0.71')
    expect(line).toContain('1148 decided claims')
    expect(line).toContain('decided rate 0.35')
    expect(line).toContain('over 7 days')
  })

  it('says "not graded yet" in words rather than 0.00', () => {
    // 0.00 on a badge reads as a measured failure — the same discipline the
    // fidelity arm already keeps.
    expect(standingSentence(null, 'The voice')).toContain('not graded against the world yet')
    const unmeasured = standingSentence(
      block({ value: null, state: 'unmeasured', state_note: null }),
      'The voice',
    )
    expect(unmeasured).toContain('not graded against the world yet')
    expect(unmeasured).not.toContain('0.00')
  })

  it('withholds the number entirely when the graders disagreed', () => {
    const limited = standingSentence(
      block({ value: null, state: 'instrument_limited', instrument_limited: true }),
      'The record',
    )
    expect(limited).toContain('did not agree with each other often enough')
    expect(limited).not.toMatch(/\d\.\d\d/)
  })

  it('names the sample fraction when the day was sampled', () => {
    const sampled = standingSentence(
      block({ state: 'sampled', sample_fraction: 0.4 }),
      'The record',
    )
    expect(sampled).toContain('0.71')
    expect(sampled).toContain('40% sample')
  })

  it('renders the server-composed state_note verbatim when one is served', () => {
    // One sentence per state, composed in one place — the reader and the API
    // cannot drift into two different wordings of the same fact.
    expect(
      standingSentence(
        block({ value: null, state: 'unmeasured', state_note: 'the server said this' }),
        'The voice',
      ),
    ).toBe('the server said this')
  })

  it('projectStanding is null-in null-out', () => {
    expect(projectStanding(null)).toBeNull()
    expect(projectStanding('nope')).toBeNull()
  })
})

// ── W-3 — the Assessment's citations all resolve to ONE uuid ────────────────
describe('the Assessment citation chain (W-3)', () => {
  it('projects every fenced citation off the live double-nested shape', () => {
    const cites = projectAssessmentCitations(assessmentCitations)
    expect(cites).toHaveLength(4)
    // THE FENCE — D-6 says `derived_from == [spine_id]`, so every ref_id IS the
    // spine. That was never the defect and nothing here widens it.
    expect(new Set(cites.map((c) => c.ref_id)).size).toBe(1)
    // ...and the ordinal is what tells them apart.
    expect(cites.map((c) => c.ordinal)).toEqual([1, 2, 3, 7])
    expect(cites.every((c) => c.spine_block !== null)).toBe(true)
    expect(cites[0].spine_block!.desk).toBe('escalation_composition')
    expect(cites[3].spine_block!.target_id).toBe('country_g20_us')
  })

  it('drops an entry with no ordinal rather than resolving it to nothing', () => {
    const row = {
      id: 'x',
      produced_at: '2026-09-06T12:15:00+00:00',
      data: { data: { citations: [{ marker: '[[ref:1]]', ref_id: 'spine' }] } },
    } as unknown as ReadFindingRow
    expect(projectAssessmentCitations(row)).toEqual([])
  })

  it('resolves each citation into Assessment → block → desk read → signals', () => {
    const spine = projectAssembly(world)!
    const chains = resolveAssessmentCitations(
      projectAssessmentCitations(assessmentCitations),
      spine,
      'World read — 2026-09-06',
    )
    expect(chains).toHaveLength(4)
    const first = chains[0]
    expect(first.spine_id).toBe(projectAssessmentCitations(assessmentCitations)[0].ref_id)
    expect(first.spine_title).toBe('World read — 2026-09-06')
    expect(first.in_spine).toBe(true)
    expect(first.block!.ordinal).toBe(1)
    // Hop 3 — the chain terminates in the world, not in another of our rows.
    // The fixture's signal ids are null (a pre-salience cycle), so the hop is
    // asserted on what the block actually carries: a marker and a title.
    expect(first.signals.length).toBeGreaterThan(0)
    expect(first.signals[0].marker).toBeTruthy()
    expect(first.signals[0].title).toBeTruthy()
    // The citation's OWN stamp is what names the desk head: it survives a spine
    // that has since been superseded, and the fixtures here are two cycles.
    expect(first.block_desk).toBe('escalation_composition')
  })

  it('states an ordinal the spine does not carry rather than faking a block', () => {
    const chains = resolveAssessmentCitations(
      [
        {
          marker: '[[ref:99]]',
          ordinal: 99,
          ref_id: 'spine-uuid',
          ref_kind: 'finding',
          source: 'world_assessor',
          title: 'Country composition',
          spine_block: { finding_id: 'head-uuid', desk: 'country_composition', target_id: 'country_g20_pk' },
          tier: null,
          effective_confidence: null,
        },
      ],
      projectAssembly(world),
    )
    expect(chains[0].in_spine).toBe(false)
    expect(chains[0].block).toBeNull()
    expect(chains[0].signals).toEqual([])
    // The hops the CITATION knows still resolve — a shorter chain, never a
    // wrong one.
    expect(chains[0].block_desk).toBe('country_composition')
    expect(chains[0].block_finding_id).toBe('head-uuid')
  })

  it('resolves hop 1 and hop 2 even with no spine loaded at all', () => {
    const chains = resolveAssessmentCitations(
      projectAssessmentCitations(assessmentCitations),
      null,
    )
    expect(chains).toHaveLength(4)
    expect(chains.every((c) => c.in_spine === false)).toBe(true)
    expect(chains.every((c) => c.spine_id !== '')).toBe(true)
    expect(chains[0].block_desk).toBe('escalation_composition')
  })
})
