/**
 * Tests for the CROSS-FRAMING derivation (wave P lane B).
 *
 * What is actually at stake here is not arithmetic — this module computes no
 * statistic — it is whether each row says the RIGHT TRUE THING. So the suite is
 * organised by row STATE rather than by function, and each case pins the one
 * sentence that distinguishes it from the state next to it:
 *
 *   * `frames` vs `silent`   — a unit that cites the claim's own signal is
 *     framing it; a unit that cites none of them is ABSENT on this matter and
 *     must never be rendered as disagreeing with it.
 *   * `absent` vs `unrecorded` — a unit with a typed absence gets the route's
 *     own words, a proof and a clock; a unit with none says "no typed absence
 *     is recorded", which is a real answer and not a blank.
 *   * a STALE absence says "last known absence, not re-checked" — the whole
 *     difference between an old absence and a current one.
 *   * a lens that ran and cited nothing is SILENT, not dissenting.
 *   * every distinguishable "no divergence receipt" is its own answer: no
 *     response, a failed read, no run ever, and a run that did not resolve this
 *     desk are four findings, not one grey blank.
 *
 * The fixtures are shaped off the LIVE payloads read on 2026-09-26 from
 * `/findings?analyst_id=country_composition`, `/v3/contentions?scope=`,
 * `/v3/absence?scope=`, `/v3/layers/divergence` and `/journal?kind=lens`.
 */
import { describe, it, expect } from 'vitest'

import type { AbsenceItem, AbsenceResponse } from '@/lib/absenceModel'
import type { AssemblyPayload, AssemblyBlock } from '@/lib/assemblyModel'
import type { ContentionRow } from '@/lib/contentionsModel'
import type { JournalEntry, LayerDivergenceResponse } from '@/lib/api'
import type { Citation } from '@/lib/citationsModel'
import {
  LEAN_LENSES,
  absenceSentence,
  bearingContentions,
  citationsByOrdinal,
  countsSentence,
  crossFramingScope,
  deriveCrossFraming,
  deriveDivergence,
  deriveLensRows,
  deriveSilence,
  deriveUnitRows,
  pickClaim,
  resolveClaim,
  unitLabel,
} from '@/lib/framingModel'
import { SCOPE_PIN_ORIGIN } from '@/lib/consultContext'
import { scopePin, scopeSummary } from '@/lib/consultContext'

// ---------------------------------------------------------------------------
// Fixtures — shaped off the live payloads, trimmed to what the model reads.
// ---------------------------------------------------------------------------

const SIG_SHARED = '52c57896-9f53-4865-a56e-e36723573696'
const SIG_OTHER = '001e85c2-ede0-4015-8789-3a32f79b1a76'
const HEAD_ENERGY = 'ab9cd76f-dbce-4b41-80f6-9156f60018a3'
const HEAD_MILITARY = '6d6c4cab-5d6c-43cd-a6ab-4b1cb1294af3'
const RECORD = 'b3edd290-f02d-44bc-84f2-3c695c53c508'

function block(over: Partial<AssemblyBlock> & { ordinal: number; desk: string }): AssemblyBlock {
  return {
    finding_id: `head-${over.desk}`,
    target_id: 'country_g20_it',
    target_name: 'country_g20_it',
    question: over.desk,
    question_source: 'fallback_desk_name',
    produced_at: '2026-09-26T04:01:15.233314+00:00',
    evidence_age_h: 7.5,
    tier: 'basis',
    severity: 'elevated',
    verify: {
      overall_score: 0.857,
      effective_confidence: 0.857,
      checkable_claims: 7,
      supported_claims: 6,
      judge_status: 'unavailable',
      score_state: 'scored',
    },
    salience: null,
    spans: [],
    contextSpans: [],
    signals: [],
    corroboration: { n_sources: 2, n_desks_sharing: 1 },
    ...over,
  }
}

function span(text: string, markers: string[], headId: string, role = 'bluf') {
  return {
    role: role as never,
    text,
    origin: { head_id: headId, start: 0, end: text.length, body_sha256: 'x', body_len: 100 },
    markers,
    scope_tokens: [],
  }
}

function signal(marker: string, id: string, alsoCitedBy: { desk: string; target_id: string | null }[] = []) {
  return {
    marker,
    signal_id: id,
    source_id: 'source.example',
    title: 'a signal',
    url: null,
    age_h: 3,
    salience_magnitude: 0.4,
    also_cited_by: alsoCitedBy,
  }
}

/**
 * A desk record with five roster units in four different postures:
 *  1 energy_security    — carries the claim
 *  2 escalation         — cites the SAME signal → frames it
 *  3 military_posture   — cites a different signal → read, silent on it
 *  4 economic_coercion  — no block, a typed absence names it
 *  5 proliferation_watch— no block, no typed absence
 */
function assembly(over: Partial<AssemblyPayload> = {}): AssemblyPayload {
  return {
    schema: 'assembly.v1',
    regime: 'assembly',
    tier: 'country',
    as_of: '2026-09-26T11:34:57.966415+00:00',
    lead: null,
    blocks: [
      block({
        ordinal: 1,
        desk: 'energy_security',
        finding_id: HEAD_ENERGY,
        spans: [
          span('Italy’s energy-security pressure remains elevated [92].', ['[92]'], HEAD_ENERGY),
        ],
        signals: [signal('[92]', SIG_SHARED, [{ desk: 'escalation', target_id: 'country_g20_it' }])],
      }),
      block({
        ordinal: 2,
        desk: 'escalation',
        finding_id: 'head-escalation',
        spans: [span('The same fuel shock reads as coercive leverage [4].', ['[4]'], 'head-escalation')],
        signals: [signal('[4]', SIG_SHARED)],
      }),
      block({
        ordinal: 3,
        desk: 'military_posture',
        finding_id: HEAD_MILITARY,
        spans: [span('Italy commissions a fifth PPA combat ship [35].', ['[35]'], HEAD_MILITARY)],
        signals: [signal('[35]', SIG_OTHER)],
      }),
    ],
    tensions: [],
    tension_checked: {
      pairs_examined: 21,
      pairs_found: 0,
      pairs_found_carried: 0,
      pairs_found_uncarried: 0,
      scope: 'shown_only',
      scope_note: 'BLUF-grain; ambivalent pairs declined',
    },
    drops: {
      shown_not_carried: [],
      not_selected: [],
      trimmed: [],
      below_floor: [],
      no_head: ['proliferation_watch'],
      counts: {
        shown: 3,
        carried: 3,
        shown_not_carried: 0,
        candidates: 3,
        not_selected: 0,
        trimmed: 0,
        below_floor: 0,
        no_head: 1,
        invisible_heads: 0,
      },
      why_classes: ['no_head_in_horizon'],
    },
    coverage: [
      { unit: 'energy_security', status: 'in_basis', read_date: '26 September 04:01 UTC', age_h: 7.56 },
      { unit: 'escalation', status: 'in_basis', read_date: '26 September 04:18 UTC', age_h: 7.27 },
      { unit: 'military_posture', status: 'in_basis', read_date: '26 September 04:19 UTC', age_h: 7.26 },
      { unit: 'economic_coercion', status: 'no_head_in_horizon', read_date: null, age_h: null },
      { unit: 'proliferation_watch', status: 'no_head_in_horizon', read_date: null, age_h: null },
    ],
    connectives: { vocabulary_version: 'connective.v1' },
    ...over,
  }
}

function absenceItem(over: Partial<AbsenceItem> & { subject: string }): AbsenceItem {
  return {
    kind: 'not_collected',
    since: '2026-09-19T00:00:00Z',
    window: null,
    reason: 'no source in the desk roster carries this unit’s subject',
    as_of: '2026-09-19T00:00:00Z',
    as_of_basis: 'the collection scan for this desk',
    expires_at: '2026-09-20T00:00:00Z',
    review: null,
    stale: false,
    proof: {
      what_was_checked: 'the desk’s registered feed set',
      checked_at: '2026-09-19T00:00:00Z',
      ref: 'aaaabbbb-0000-1111-2222-333344445555',
      ref_kind: 'finding',
    },
    ...over,
  }
}

function absence(items: AbsenceItem[], notMeasured: string[] = []): AbsenceResponse {
  return {
    version: 'absence.v1',
    scope: 'country_g20_it',
    read_at: '2026-09-26T12:00:00Z',
    kinds: {},
    absences: items,
    not_measured: notMeasured,
  }
}

function contention(over: Partial<ContentionRow> = {}): ContentionRow {
  return {
    claim_id: '69f7db5184059b8be3e8dc706e08bce620808503022fe248286fa72d47b34320',
    claim_text: 'Italy’s energy-security pressure stays elevated [2][63].',
    finding_id: RECORD,
    origin_head_id: HEAD_ENERGY,
    block_ordinal: 1,
    span_role: 'bluf',
    target_id: 'country_g20_it',
    desk_key: 'energy_security',
    analyst_id: 'country_composition',
    query: 'Italy energy security pressure decreased 2026',
    query_source: 'model',
    query_novel_tokens: 2,
    stance: 'contradicts',
    derivation: 'polarity',
    reason: 'a page we hold states the opposite',
    statement: '',
    rung: 'search.searxng.local',
    refs: [],
    linked_signals: 0,
    pipeline_version: '2026-09/7a.2',
    retrieved_at: '2026-09-25 23:57:53+00',
    as_of: '2026-09-25 23:57:53+00',
    expires_at: '2026-10-02 23:57:53+00',
    live: true,
    ...over,
  }
}

function view(over: Parameters<typeof deriveCrossFraming>[0] = { targetId: 'country_g20_it', recordId: RECORD, assembly: assembly() }) {
  const v = deriveCrossFraming(over)
  if (!v) throw new Error('fixture produced no view')
  return v
}

// ---------------------------------------------------------------------------

describe('pickClaim', () => {
  it('takes the claim the caller asked for, by contention claim_id', () => {
    const rows = [contention({ block_ordinal: 3, span_role: 'bluf', claim_id: 'wanted' })]
    const got = pickClaim(assembly(), { claimId: 'wanted', contentions: rows })
    expect(got).toEqual({ ref: { ordinal: 3, spanIndex: 0 }, chosenBy: 'requested' })
  })

  it('takes the claim the caller asked for, by sentence — markers and spacing ignored', () => {
    const got = pickClaim(assembly(), {
      claimText: '  Italy commissions a  fifth PPA combat ship  ',
    })
    expect(got?.ref.ordinal).toBe(3)
    expect(got?.chosenBy).toBe('requested')
  })

  it('falls back to the desk’s MOST CONTESTED claim, contradicts before qualifies', () => {
    const rows = [
      contention({ block_ordinal: 3, stance: 'qualifies', as_of: '2026-09-26 01:00:00+00' }),
      contention({ block_ordinal: 2, stance: 'contradicts', as_of: '2026-09-20 01:00:00+00' }),
    ]
    const got = pickClaim(assembly(), { contentions: rows })
    expect(got).toEqual({ ref: { ordinal: 2, spanIndex: 0 }, chosenBy: 'most-contested' })
  })

  it('ignores a contention that settled nothing — none_found is the common case', () => {
    const rows = [contention({ block_ordinal: 3, stance: 'none_found', derivation: 'none' })]
    const got = pickClaim(assembly(), { contentions: rows })
    expect(got?.chosenBy).toBe('first-block')
    expect(bearingContentions(rows)).toEqual([])
  })

  it('returns null for a record with no quoted span rather than an empty claim', () => {
    const a = assembly({ blocks: [block({ ordinal: 1, desk: 'energy_security', spans: [] })] })
    expect(pickClaim(a, {})).toBeNull()
    expect(deriveCrossFraming({ targetId: 'country_g20_it', recordId: RECORD, assembly: a })).toBeNull()
  })

  it('returns null when there is no record at all', () => {
    expect(
      deriveCrossFraming({ targetId: 'country_g20_it', recordId: RECORD, assembly: null }),
    ).toBeNull()
  })
})

describe('resolveClaim', () => {
  it('carries the sentence VERBATIM, with its own markers and its cited signals', () => {
    const a = assembly()
    const claim = resolveClaim(a, RECORD, { ref: { ordinal: 1, spanIndex: 0 }, chosenBy: 'requested' }, [])
    expect(claim?.text).toBe('Italy’s energy-security pressure remains elevated [92].')
    expect(claim?.markers).toEqual(['[92]'])
    expect(claim?.signalIds).toEqual([SIG_SHARED])
    expect(claim?.unit).toBe('energy_security')
    expect(claim?.headId).toBe(HEAD_ENERGY)
    expect(claim?.asOf).toBe(a.as_of)
  })
})

describe('the unit table — one row per roster unit, never a blank', () => {
  const a = assembly()
  const claim = resolveClaim(a, RECORD, { ref: { ordinal: 1, spanIndex: 0 }, chosenBy: 'requested' }, [])!

  it('states the carrier, the framer, and the unit that is silent on this claim', () => {
    const rows = deriveUnitRows(a, claim, null, [])
    const byUnit = new Map(rows.map((r) => [r.unit, r]))
    expect(byUnit.get('energy_security')!.state).toBe('carries')
    // `escalation` cites the very same signal id — the producer's own stamp.
    expect(byUnit.get('escalation')!.state).toBe('frames')
    expect(byUnit.get('escalation')!.sharedSignalIds).toEqual([SIG_SHARED])
    expect(byUnit.get('escalation')!.framing).toBe(
      'The same fuel shock reads as coercive leverage [4].',
    )
    // `military_posture` cites a DIFFERENT signal: it is absent on this matter.
    expect(byUnit.get('military_posture')!.state).toBe('silent')
    expect(byUnit.get('military_posture')!.sharedSignalIds).toEqual([])
  })

  it('covers every unit on the record’s own roster, and puts the carrier first', () => {
    const rows = deriveUnitRows(a, claim, null, [])
    expect(rows).toHaveLength(a.coverage.length)
    expect(rows[0].state).toBe('carries')
    expect(rows.every((r) => r.framing !== null || r.absenceLine !== null || r.state === 'unrecorded')).toBe(true)
  })

  it('a unit with no read and a typed absence renders the ROUTE’s own words, with its proof', () => {
    const rows = deriveUnitRows(
      a,
      claim,
      absence([absenceItem({ subject: 'economic_coercion' })]),
      [],
    )
    const row = rows.find((r) => r.unit === 'economic_coercion')!
    expect(row.state).toBe('absent')
    expect(row.framing).toBeNull()
    expect(row.absenceLine).toContain('not collected')
    expect(row.absenceLine).toContain('no source in the desk roster carries')
    expect(row.absence?.proof.what_was_checked).toBe('the desk’s registered feed set')
    expect(absenceSentence(row)).toContain('measured')
  })

  it('a STALE absence says last known, not re-checked — never passes for current', () => {
    const rows = deriveUnitRows(
      a,
      claim,
      absence([
        absenceItem({
          subject: 'economic_coercion',
          stale: true,
          expires_at: '2026-09-20T00:00:00Z',
        }),
      ]),
      [],
    )
    const row = rows.find((r) => r.unit === 'economic_coercion')!
    expect(row.state).toBe('absent')
    expect(row.absenceStale).toBe(true)
    expect(absenceSentence(row)).toContain('last known absence, not re-checked')
  })

  it('a unit with no read and NO typed absence says so — the state a blank used to hide', () => {
    const rows = deriveUnitRows(a, claim, absence([]), [])
    const row = rows.find((r) => r.unit === 'proliferation_watch')!
    expect(row.state).toBe('unrecorded')
    expect(row.absence).toBeNull()
    expect(absenceSentence(row)).toContain('no typed absence is recorded')
    // …and it still carries the record's OWN word for why there is no head.
    expect(absenceSentence(row)).toContain('no_head_in_horizon')
  })

  it('never fabricates a figure: an absent row measures nothing, and nothing reads as 0', () => {
    const rows = deriveUnitRows(a, claim, absence([]), [])
    const row = rows.find((r) => r.unit === 'proliferation_watch')!
    expect(row.faithfulness).toBeNull()
    expect(row.citedCount).toBeNull()
    expect(row.nSources).toBeNull()
    expect(row.foldChips).toEqual([])
  })

  it('carries the block’s OWN faithfulness — never recomputed, null when unstated', () => {
    const rows = deriveUnitRows(a, claim, null, [])
    expect(rows.find((r) => r.unit === 'escalation')!.faithfulness).toBe(0.857)
    const bare = assembly({
      blocks: [
        block({
          ordinal: 1,
          desk: 'energy_security',
          verify: null,
          spans: [span('a claim [1].', ['[1]'], HEAD_ENERGY)],
          signals: [signal('[1]', SIG_SHARED)],
        }),
      ],
    })
    const bareClaim = resolveClaim(bare, RECORD, { ref: { ordinal: 1, spanIndex: 0 }, chosenBy: 'first-block' }, [])!
    expect(deriveUnitRows(bare, bareClaim, null, [])[0].faithfulness).toBeNull()
  })

  it('a contested unit carries the retrieval record — and the chip is the reading kit’s own', () => {
    const rows = deriveUnitRows(a, claim, null, [contention({ block_ordinal: 2 })])
    const row = rows.find((r) => r.unit === 'escalation')!
    expect(row.contested?.stance).toBe('contradicts')
    // With the record's citation in hand the fold chip comes from `claimFold`,
    // so the vocabulary is identical to the one beside a claim in the Inspector.
    const citations: Citation[] = [
      { marker: '[[ref:2]]', refId: 'head-escalation', refKind: 'finding', signalId: 'head-escalation' },
    ]
    const withChips = deriveUnitRows(a, claim, null, [contention({ block_ordinal: 2 })], {
      citations,
    })
    const chipped = withChips.find((r) => r.unit === 'escalation')!
    expect(chipped.foldChips.map((c) => c.kind)).toContain('contested-by-retrieval')
    expect(chipped.foldChips[0].label).toBe('contested by retrieval')
  })

  it('a unit the producer named in also_cited_by frames the claim even without its own block signal', () => {
    const a2 = assembly({
      blocks: [
        block({
          ordinal: 1,
          desk: 'energy_security',
          spans: [span('a claim [1].', ['[1]'], HEAD_ENERGY)],
          signals: [
            signal('[1]', SIG_SHARED, [{ desk: 'military_posture', target_id: 'country_g20_it' }]),
          ],
        }),
        block({
          ordinal: 3,
          desk: 'military_posture',
          spans: [span('another sentence [9].', ['[9]'], HEAD_MILITARY)],
          signals: [signal('[9]', SIG_OTHER)],
        }),
      ],
    })
    const c2 = resolveClaim(a2, RECORD, { ref: { ordinal: 1, spanIndex: 0 }, chosenBy: 'first-block' }, [])!
    const rows = deriveUnitRows(a2, c2, null, [])
    expect(rows.find((r) => r.unit === 'military_posture')!.state).toBe('frames')
  })

  it('surfaces a declared tension between the claim’s unit and another', () => {
    const a2 = assembly({
      tensions: [
        {
          kind: 'carried_pair',
          a: { ordinal: 1, span_index: 0 },
          b: { ordinal: 2, span_index: 0 },
          b_ref: null,
          b_carried: true,
          b_why: null,
          statement: 'the two reads disagree on direction',
          statement_source: 'template',
          detector: 'polarity',
          same_target: true,
          same_window_h: 72,
        },
      ],
    })
    const c2 = resolveClaim(a2, RECORD, { ref: { ordinal: 1, spanIndex: 0 }, chosenBy: 'first-block' }, [])!
    const rows = deriveUnitRows(a2, c2, null, [])
    expect(rows.find((r) => r.unit === 'escalation')!.tensions).toHaveLength(1)
    expect(rows.find((r) => r.unit === 'military_posture')!.tensions).toEqual([])
  })

  it('names a unit off the gap-strip roster honestly rather than dropping it', () => {
    expect(unitLabel('energy_security')).toBe('Energy security')
    expect(unitLabel('some_future_unit')).toBe('Some Future Unit')
    expect(unitLabel('energy_security', { unit: 'energy_security', unit_name: 'Energia', status: 'in_basis', read_date: null, age_h: null })).toBe('Energia')
  })
})

describe('the counts', () => {
  it('every number says what it counts, against the record’s own roster', () => {
    const v = view({
      targetId: 'country_g20_it',
      recordId: RECORD,
      assembly: assembly(),
      absence: absence([absenceItem({ subject: 'economic_coercion' })]),
    })
    expect(v.counts).toEqual({ carriedBy: 2, roster: 5, silent: 1, noRead: 2 })
    expect(countsSentence(v.counts)).toBe(
      'carried by 2 of 5 units · 2 with no read · 1 read this desk but not this claim',
    )
  })

  it('omits the silent clause when nothing is in it — never prints a bare 0', () => {
    expect(countsSentence({ carriedBy: 3, roster: 3, silent: 0, noRead: 0 })).toBe(
      'carried by 3 of 3 units · 0 with no read',
    )
  })
})

describe('the claim’s verified state', () => {
  it('an unverified record reads not-recorded, not "failed"', () => {
    const v = view()
    expect(v.verdict.kind).toBe('not-recorded')
  })

  it('reads the per-claim ledger for the claim’s own ordinal', () => {
    const v = view({
      targetId: 'country_g20_it',
      recordId: RECORD,
      assembly: assembly(),
      verification: {
        judge_status: 'llm',
        claim_verdicts: [{ text: 'the claim', markers: [1], verdict: 'supported' }],
      },
    })
    expect(v.verdict.kind).toBe('supported')
  })
})

describe('the lens row — the six leans on the same evidence', () => {
  const a = assembly()
  const claim = resolveClaim(a, RECORD, { ref: { ordinal: 1, spanIndex: 0 }, chosenBy: 'requested' }, [])!

  function lens(over: Partial<JournalEntry> & { analyst_id: string }): JournalEntry {
    return {
      id: `entry-${over.analyst_id}`,
      entry_kind: 'lens',
      title: 'Read',
      body: '',
      claims: [],
      cited_substrate_refs: [],
      honesty_flags: ['forecast_unproven'],
      period_start: '2026-09-25T00:00:00Z',
      period_end: '2026-09-26T00:00:00Z',
      produced_at: '2026-09-26T11:30:47Z',
      analyst_version: '1',
      verify_score: 0.8333,
      verify_body: null,
      ...over,
    }
  }

  it('is the six declared leans, each with its prior named', () => {
    expect(LEAN_LENSES).toHaveLength(6)
    const rows = deriveLensRows([], claim)
    expect(rows.map((r) => r.analystId)).toEqual(LEAN_LENSES.map((l) => l.id))
    expect(rows.every((r) => r.prior !== null && r.name !== null)).toBe(true)
  })

  it('a lens citing one of the claim’s signals shows its OWN sentence and its own score', () => {
    const rows = deriveLensRows(
      [
        lens({
          analyst_id: 'lens_militarist',
          claims: [
            { text_span: 'the fuel shock is leverage, not scarcity', kind: 'fact', refs: [{ id: SIG_SHARED, kind: 'signal' }] },
          ],
        }),
      ],
      claim,
    )
    const row = rows.find((r) => r.analystId === 'lens_militarist')!
    expect(row.framing).toBe('the fuel shock is leverage, not scarcity')
    expect(row.citedRefId).toBe(SIG_SHARED)
    expect(row.judgeScore).toBeCloseTo(0.8333)
    expect(row.name).toBe('Strategist')
    expect(row.prior).toBe('who holds the next rung')
    expect(row.readButSilent).toBe(false)
  })

  it('a lens that ran and cited none of the claim’s refs is SILENT, not dissenting', () => {
    const rows = deriveLensRows(
      [
        lens({
          analyst_id: 'lens_left',
          claims: [{ text_span: 'about something else', kind: 'fact', refs: [{ id: 'unrelated', kind: 'signal' }] }],
        }),
      ],
      claim,
    )
    const row = rows.find((r) => r.analystId === 'lens_left')!
    expect(row.entryId).toBe('entry-lens_left')
    expect(row.framing).toBeNull()
    expect(row.readButSilent).toBe(true)
  })

  it('a lens with no read at all carries a null entry — never a fabricated score', () => {
    const row = deriveLensRows([], claim).find((r) => r.analystId === 'lens_centre')!
    expect(row.entryId).toBeNull()
    expect(row.judgeScore).toBeNull()
    expect(row.readButSilent).toBe(false)
  })

  it('keeps only the NEWEST entry per lens, and ignores non-lens journal kinds', () => {
    const rows = deriveLensRows(
      [
        lens({ analyst_id: 'lens_right', produced_at: '2026-09-20T00:00:00Z', verify_score: 0.1 }),
        lens({ analyst_id: 'lens_right', produced_at: '2026-09-26T00:00:00Z', verify_score: 0.9 }),
        lens({ analyst_id: 'journal_voice', entry_kind: 'entry', verify_score: 0.5 }),
      ],
      claim,
    )
    expect(rows.find((r) => r.analystId === 'lens_right')!.judgeScore).toBe(0.9)
    expect(rows.find((r) => r.analystId === 'journal_voice')).toBeUndefined()
  })

  it('shows a lens persona this bundle has not learned about, under its own id', () => {
    const rows = deriveLensRows([lens({ analyst_id: 'lens_brand_new' })], claim)
    const row = rows.find((r) => r.analystId === 'lens_brand_new')!
    expect(row.name).toBeNull()
    expect(row.prior).toBeNull()
    expect(row.entryId).toBe('entry-lens_brand_new')
  })
})

describe('where the layers diverge — four distinguishable absences', () => {
  function divergence(over: Partial<LayerDivergenceResponse> = {}): LayerDivergenceResponse {
    return {
      measured: true,
      generated_at: '2026-09-26T12:00:00Z',
      reader_version: 'r1',
      unit_sentence: 'Per country, the same stack of source layers is counted per day…',
      as_of: '2026-09-26',
      receipt_run_id: 'run-1',
      run_started_at: '2026-09-26T00:10:00Z',
      method_version: 'layer_divergence/2026-09.1',
      payload_schema: 'layer_divergence.v1',
      classification_audit: 'the source→layer map is un-audited (SEAMS #60).',
      window_days: 28,
      baseline_days: 28,
      z_threshold: 2,
      mad_floor: 0.1,
      consecutive_days: 2,
      thin_min_per_day: 3,
      layer_vocab: ['official', 'social_digest'],
      pairs_declared: [
        {
          pair_id: 'regime_public_gap',
          layer_a: 'official',
          layer_b: 'social_digest',
          meaning: 'the regime-public gap',
        },
      ],
      desks: [
        {
          target_id: 'country_g20_it',
          country: 'IT',
          map_version: 'v1',
          sources_mapped: 12,
          rows_scanned: 100,
          rows_truncated: false,
          aperture: {},
          counts_suppressed_by_aperture: 0,
          layers: {},
          pairs: [
            {
              pair_id: 'regime_public_gap',
              layer_a: 'official',
              layer_b: 'social_digest',
              evaluable: true,
              no_fire_reason: 'below_threshold',
            },
          ],
          fired: null,
        } as never,
      ],
      desks_unresolved: [],
      warnings: [],
      ...over,
    }
  }

  it('resolves the desk’s pairs verbatim, with the map’s own classifier', () => {
    const d = deriveDivergence(divergence(), 'country_g20_it')
    expect(d.absent).toBeNull()
    expect(d.pairs).toHaveLength(1)
    expect(d.pairs[0].state).toBe('below_threshold')
    expect(d.pairs[0].meaning).toBe('the regime-public gap')
    expect(d.classificationAudit).toContain('SEAMS #60')
  })

  it('no response is not a measurement of zero divergence', () => {
    expect(deriveDivergence(null, 'country_g20_it').absent).toBe('no-response')
  })

  it('a FAILED read is its own answer, not a quiet instrument', () => {
    expect(deriveDivergence(divergence({ measured: false }), 'country_g20_it').absent).toBe(
      'not-measured',
    )
  })

  it('no run ever is distinct from a run that measured nothing', () => {
    expect(deriveDivergence(divergence({ receipt_run_id: null }), 'country_g20_it').absent).toBe(
      'no-run',
    )
  })

  it('a desk the run did not resolve was not measured — not measured as agreeing', () => {
    expect(deriveDivergence(divergence(), 'country_g20_xx').absent).toBe('desk-not-resolved')
  })
})

describe('what no unit says', () => {
  const a = assembly()
  const claim = resolveClaim(a, RECORD, { ref: { ordinal: 1, spanIndex: 0 }, chosenBy: 'requested' }, [])!

  it('names the units nothing carries, absent rather than contradicted', () => {
    const rows = deriveUnitRows(a, claim, absence([]), [])
    const s = deriveSilence(a, rows, absence([]))
    expect(s.unnamedUnits.sort()).toEqual(['economic_coercion', 'proliferation_watch'])
    expect(s.empty).toBe(false)
  })

  it('carries the desk’s OFF-ROSTER absences — the subjects no unit speaks to', () => {
    const resp = absence([
      absenceItem({ subject: 'economic_coercion' }),
      absenceItem({
        subject: '510b1c1760d424f8437a68b5be1d34d3',
        kind: 'searched_found_nothing',
        reason: 'the external audit searched and nothing decided this claim',
        stale: true,
      }),
    ])
    const rows = deriveUnitRows(a, claim, resp, [])
    const s = deriveSilence(a, rows, resp)
    expect(s.offRosterAbsences).toHaveLength(1)
    expect(s.offRosterAbsences[0].kindLabel).toBe('searched, found nothing')
    expect(s.offRosterAbsences[0].stale).toBe(true)
    // the unit-keyed one stays on the unit row, not duplicated down here
    expect(s.offRosterAbsences.map((x) => x.subject)).not.toContain('economic_coercion')
  })

  it('repeats the route’s own unread-kind reasons verbatim', () => {
    const resp = absence([], ['no loaded collection names this desk'])
    const s = deriveSilence(a, deriveUnitRows(a, claim, resp, []), resp)
    expect(s.notMeasured).toEqual(['no loaded collection names this desk'])
  })

  it('carries the record’s own drop ledger, by why-class', () => {
    const s = deriveSilence(a, deriveUnitRows(a, claim, null, []), null)
    expect(s.drops).toEqual([{ why: 'no_head_in_horizon', rows: ['proliferation_watch'] }])
  })

  it('states the tension pass’s CHECKED NEGATIVE — 21 examined, 0 found', () => {
    const s = deriveSilence(a, deriveUnitRows(a, claim, null, []), null)
    expect(s.tensionsExamined).toBe(21)
    expect(s.tensionsFound).toBe(0)
    expect(s.tensionScopeNote).toBe('BLUF-grain; ambivalent pairs declined')
  })

  it('a record with nothing unsaid says so, rather than rendering an empty box', () => {
    const full = assembly({
      coverage: [
        { unit: 'energy_security', status: 'in_basis', read_date: null, age_h: 1 },
        { unit: 'escalation', status: 'in_basis', read_date: null, age_h: 1 },
        { unit: 'military_posture', status: 'in_basis', read_date: null, age_h: 1 },
      ],
      drops: null,
      tension_checked: null,
    })
    const c = resolveClaim(full, RECORD, { ref: { ordinal: 1, spanIndex: 0 }, chosenBy: 'first-block' }, [])!
    const s = deriveSilence(full, deriveUnitRows(full, c, absence([]), []), absence([]))
    expect(s.empty).toBe(true)
  })
})

describe('citationsByOrdinal', () => {
  it('keys a composition citation by its [[ref:N]] ordinal, first wins', () => {
    const map = citationsByOrdinal([
      { marker: '[[ref:2]]', refId: 'a', refKind: 'finding', signalId: 'a' },
      { marker: '[[ref:2]]', refId: 'b', refKind: 'finding', signalId: 'b' },
      { marker: 'no-digits', refId: 'c', refKind: 'finding', signalId: 'c' },
    ])
    expect(map.get(2)?.refId).toBe('a')
    expect(map.size).toBe(1)
  })
})

describe('the consult scope pin', () => {
  const v = view()

  it('is the AMBIENT pin — the same origin Consult auto-manages', () => {
    const scope = crossFramingScope(v)
    const pin = scopePin(scope)
    expect(pin.origin).toBe(SCOPE_PIN_ORIGIN)
    expect(pin.kind).toBe('report')
    expect(pin.id).toBe(RECORD)
  })

  it('names the claim in the label and resolves the id to a real record', () => {
    const scope = crossFramingScope(v)
    expect(scope.id).toBe(RECORD)
    expect(scope.label).toContain('Energy security')
    expect(scope.label).toContain('Italy’s energy-security pressure remains elevated')
    expect(scope.origin).toBe('cross-framing')
  })

  it('carries the claim’s own aperture, so the census line reports what it rested on', () => {
    const scope = crossFramingScope(v)
    expect(scope.members.findingIds).toEqual([RECORD, HEAD_ENERGY])
    expect(scope.members.targetIds).toEqual(['country_g20_it'])
    expect(scope.members.signalIds).toEqual([SIG_SHARED])
    expect(scope.members.asOf).toBe(v.claim.asOf)
    const summary = scopeSummary(scope)
    expect(summary).toContain('1 desks')
    expect(summary).toContain('2 findings')
    expect(summary).toContain('1 signals')
  })

  it('trims a long claim rather than pinning a paragraph', () => {
    const long = 'x'.repeat(400)
    const a = assembly({
      blocks: [
        block({
          ordinal: 1,
          desk: 'energy_security',
          spans: [span(long, [], HEAD_ENERGY)],
          signals: [],
        }),
      ],
    })
    const v2 = view({ targetId: 'country_g20_it', recordId: RECORD, assembly: a })
    expect(crossFramingScope(v2).label.length).toBeLessThan(200)
    expect(crossFramingScope(v2).label).toContain('…')
  })
})
