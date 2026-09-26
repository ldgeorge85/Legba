/**
 * Unit test for the fold-reason derivation (Program 7, 7b-i).
 *
 * Locks the two halves:
 *  * `corroborationByOrdinal` — the record's own `blocks[].corroboration`,
 *    keyed by the ordinal its `[[ref:N]]` marker uses, read off the merged
 *    body at both envelope levels and degrading to an EMPTY map on anything
 *    malformed (the chips then fall back to the citation's own stamps).
 *  * `claimFoldChips` — every branch of the four-chip vocabulary, and the
 *    honesty contract that sits under all of them: a citation stating no fold
 *    key yields NO chips, so a read with no fold renders exactly as it did
 *    before 7b-i existed.
 */
import { describe, it, expect } from 'vitest'
import { claimFoldChips, corroborationByOrdinal, type CitationCorroboration } from './claimFold'
import type { Citation } from './citationsModel'
import { claimVerdictForMarker, type ClaimVerdict } from './claimVerdicts'

function cite(over: Partial<Citation> = {}): Citation {
  return {
    marker: '[[ref:2]]',
    refId: 'aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa',
    refKind: 'finding',
    signalId: 'aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa',
    ...over,
  }
}

function corr(over: Partial<CitationCorroboration> = {}): CitationCorroboration {
  return { nSources: 3, nIndependentSources: null, wireFolded: null, singleSource: false, ...over }
}

/** A real verify block shape (`verify.py` `FaithfulnessReport.as_dict`). */
function verification(reason: string, ordinal = 2): Record<string, unknown> {
  return {
    judge_status: 'llm',
    checkable_claims: 6,
    supported_claims: 5,
    unsupported_spans: [
      { text: 'Tehran has doubled enrichment capacity.', reason, markers: [ordinal] },
    ],
  }
}

const kinds = (chips: ReturnType<typeof claimFoldChips>) => chips.map((c) => c.kind)

// ---------------------------------------------------------------------------

describe('corroborationByOrdinal', () => {
  const body = {
    data: {
      assembly: {
        blocks: [
          {
            ordinal: 1,
            corroboration: { n_sources: 3, n_desks_sharing: 2 },
          },
          {
            ordinal: 2,
            corroboration: {
              n_sources: 3,
              n_desks_sharing: 1,
              n_independent_sources: 1,
              wire_folded: 2,
              single_source: true,
            },
          },
        ],
      },
    },
  }

  it('keys the record blocks by the ordinal their [[ref:N]] marker uses', () => {
    const map = corroborationByOrdinal(body)
    expect([...map.keys()]).toEqual([1, 2])
    expect(map.get(2)).toEqual({
      nSources: 3,
      nIndependentSources: 1,
      wireFolded: 2,
      singleSource: true,
    })
  })

  it('reads a block with no fold as the two historical keys only', () => {
    // `corroboration_block` writes the fold keys ONLY when they say something,
    // so their absence must read as null/false, never as 0 independent outlets.
    expect(corroborationByOrdinal(body).get(1)).toEqual({
      nSources: 3,
      nIndependentSources: null,
      wireFolded: null,
      singleSource: false,
    })
  })

  it('also reads a top-level assembly envelope', () => {
    const map = corroborationByOrdinal({
      assembly: { blocks: [{ ordinal: 4, corroboration: { n_sources: 1, single_source: true } }] },
    })
    expect(map.get(4)?.singleSource).toBe(true)
  })

  it('degrades to an empty map on anything that is not a block list', () => {
    expect(corroborationByOrdinal(null).size).toBe(0)
    expect(corroborationByOrdinal({}).size).toBe(0)
    expect(corroborationByOrdinal({ data: { assembly: { blocks: 'nope' } } }).size).toBe(0)
    // A block with no ordinal, or none with a corroboration block, contributes
    // nothing rather than defaulting onto ordinal 0.
    expect(
      corroborationByOrdinal({ data: { assembly: { blocks: [{ corroboration: {} }, {}] } } }).size,
    ).toBe(0)
  })
})

// ---------------------------------------------------------------------------

describe('claimFoldChips — the honesty floor', () => {
  it('yields NO chips for a citation that states no fold reason', () => {
    expect(claimFoldChips(cite())).toEqual([])
    expect(claimFoldChips(cite(), { verdict: null, corroboration: null })).toEqual([])
  })

  it('yields no chips for a record block that folded nothing', () => {
    expect(claimFoldChips(cite(), { corroboration: corr() })).toEqual([])
  })

  it('never chips an absence of measurement as a weakness', () => {
    // The two honest-absence verdicts and both positive ones are NOT reasons a
    // claim is weaker than it looks — they must not chip.
    for (const marker of ['[[ref:2]]']) {
      expect(kinds(claimFoldChips(cite(), { verdict: claimVerdictForMarker(null, marker) }))).toEqual(
        [],
      )
      expect(
        kinds(
          claimFoldChips(cite(), {
            verdict: claimVerdictForMarker({ judge_status: 'deterministic' }, marker),
          }),
        ),
      ).toEqual([])
      expect(
        kinds(
          claimFoldChips(cite(), {
            verdict: claimVerdictForMarker({ judge_status: 'llm' }, marker),
          }),
        ),
      ).toEqual([])
      expect(
        kinds(
          claimFoldChips(cite(), {
            verdict: claimVerdictForMarker(
              {
                judge_status: 'llm',
                claim_verdicts: [{ text: 'x', markers: [2], verdict: 'supported' }],
              },
              marker,
            ),
          }),
        ),
      ).toEqual([])
    }
  })
})

describe('claimFoldChips — single-source', () => {
  it('chips the citation stamp with the reason in one sentence', () => {
    const [chip] = claimFoldChips(cite({ singleSource: true }))
    expect(chip).toEqual({
      kind: 'single-source',
      label: 'single-source',
      reason: 'cited signals fold to one outlet',
      tone: 'warning',
    })
  })

  it('also chips when only the RECORD block says so', () => {
    expect(kinds(claimFoldChips(cite(), { corroboration: corr({ singleSource: true }) }))).toEqual([
      'single-source',
    ])
  })

  it('chips once when both say so', () => {
    expect(
      kinds(
        claimFoldChips(cite({ singleSource: true }), {
          corroboration: corr({ singleSource: true }),
        }),
      ),
    ).toEqual(['single-source'])
  })
})

describe('claimFoldChips — wire-folded', () => {
  it('states the COUNT when the record block carries one', () => {
    const [chip] = claimFoldChips(cite(), { corroboration: corr({ wireFolded: 2 }) })
    expect(chip.kind).toBe('wire-folded')
    expect(chip.label).toBe('2 wire-folded')
    expect(chip.reason).toBe('2 wire copies folded into one outlet')
  })

  it('singularises a fold of one', () => {
    const [chip] = claimFoldChips(cite(), { corroboration: corr({ wireFolded: 1 }) })
    expect(chip.reason).toBe('1 wire copy folded into one outlet')
  })

  it('falls back to the citation boolean, stating no number it does not have', () => {
    const [chip] = claimFoldChips(cite({ wireFolded: true }))
    expect(chip.label).toBe('wire-folded')
    expect(chip.reason).toBe('wire copies folded into one outlet')
  })

  it('prefers the record COUNT over the citation boolean', () => {
    const [chip] = claimFoldChips(cite({ wireFolded: true }), {
      corroboration: corr({ wireFolded: 3 }),
    })
    expect(chip.label).toBe('3 wire-folded')
  })

  it('does not chip a zero fold', () => {
    expect(kinds(claimFoldChips(cite(), { corroboration: corr({ wireFolded: 0 }) }))).toEqual([])
  })
})

describe('claimFoldChips — the verdict that lowered confidence', () => {
  const chipFor = (reason: string): ClaimVerdict =>
    claimVerdictForMarker(verification(reason), '[[ref:2]]')

  it('chips an unsupported verdict with the judge reason in the tooltip', () => {
    const [chip] = claimFoldChips(cite(), { verdict: chipFor('judge_unsupported') })
    expect(chip).toEqual({
      kind: 'unsupported',
      label: 'unsupported',
      reason: 'verdict: unsupported by the verify judge, confidence lowered',
      tone: 'warning',
    })
  })

  it('never understates a CONTRADICTION as "unsupported"', () => {
    const [chip] = claimFoldChips(cite(), { verdict: chipFor('judge_contradicted') })
    expect(chip.label).toBe('contradicted')
    expect(chip.tone).toBe('critical')
  })

  it('chips an advisory flag as flagged, quoting its own reason', () => {
    const [chip] = claimFoldChips(cite(), { verdict: chipFor('hedge_laundering') })
    expect(chip.label).toBe('flagged')
    expect(chip.reason).toBe(
      'verdict: asserts more confidence than the cited sub-claim, confidence lowered',
    )
  })

  it('ignores a verdict that names a DIFFERENT ordinal', () => {
    const verdict = claimVerdictForMarker(verification('judge_unsupported', 5), '[[ref:2]]')
    expect(kinds(claimFoldChips(cite(), { verdict }))).toEqual([])
  })
})

describe('claimFoldChips — insufficient evidence', () => {
  it('chips a cited head whose recorded basis is EMPTY', () => {
    const [chip] = claimFoldChips(cite({ emptyBasis: true }))
    expect(chip).toEqual({
      kind: 'insufficient-evidence',
      label: 'insufficient evidence',
      reason: 'the cited read records no basis behind this claim',
      tone: 'warning',
    })
  })

  it('says nothing when the row records a basis, or records none at all', () => {
    expect(kinds(claimFoldChips(cite({ derivedFrom: ['x'] })))).toEqual([])
    expect(kinds(claimFoldChips(cite()))).toEqual([])
  })
})

describe('claimFoldChips — ordering', () => {
  it('reads worst first: the judge verdict, the empty basis, then the folds', () => {
    const chips = claimFoldChips(
      cite({ emptyBasis: true, singleSource: true, wireFolded: true }),
      {
        verdict: claimVerdictForMarker(verification('judge_contradicted'), '[[ref:2]]'),
        corroboration: corr({ wireFolded: 2, singleSource: true }),
      },
    )
    expect(kinds(chips)).toEqual([
      'unsupported',
      'insufficient-evidence',
      'single-source',
      'wire-folded',
    ])
  })
})
