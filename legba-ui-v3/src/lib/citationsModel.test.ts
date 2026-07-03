/**
 * Unit test for the citations data layer (P1-T3).
 *
 * Locks the LIVE citation shapes: the finding payload nests its data under the
 * `data` envelope, so citations live at `body.data.citations`. Two shapes
 * coexist — a UNIT signal-ref (`signal_id`, `[N]` marker) and a COMPOSITION
 * finding-ref (`ref_id` + `ref_kind:'finding'`, `[[ref:N]]` ordinal marker).
 * Covers both, the top-level fallback, back-compat with a legacy `signal_id`
 * composition row, the honest uncited (empty) path, marker normalization, and
 * the prose tokenizer's dual-marker + unknown-marker passthrough.
 */
import { describe, it, expect } from 'vitest'
import {
  extractCitations,
  citationsByMarker,
  evidenceAnchorId,
  normalizeMarker,
  splitProse,
} from './citationsModel'

// A trimmed-down replica of a real `country_g20_us` unit finding's merged body.
const LIVE_BODY = {
  body: 'US faces a heatwave [8] and Supreme Court rulings [10].',
  title: 'US assessment',
  data: {
    category: 'national-stability',
    citations: [
      {
        marker: '[8]',
        signal_id: '50420791-b662-420b-8014-43e894c98b93',
        title: 'Extreme Heat Warning, NWS Cleveland OH',
      },
      {
        marker: '[10]',
        signal_id: '71ee7f4e-b735-4165-a6bb-220d75093661',
        title: 'US heatwave to test power grid',
        source: 'https://www.aljazeera.com/news/2026/6/30/us-heatwave',
      },
    ],
  },
}

// A composition (meta_findings_synthesizer) body — ordinal `[[ref:N]]` markers
// that cite SUB-CLAIM findings via ref_id + ref_kind.
const COMPOSITION_BODY = {
  body: 'The Gulf tension escalates [[ref:1]] while grain exports stall [[ref:2]].',
  title: 'World assessment',
  data: {
    citations: [
      {
        marker: '[[ref:1]]',
        ordinal: 1,
        ref_id: 'aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa',
        ref_kind: 'finding',
        title: 'Gulf tension sub-claim',
        evidence_text: 'Naval movements observed near the strait.',
        effective_confidence: 0.62,
        derived_from: ['aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa'],
      },
      {
        marker: '[[ref:2]]',
        ordinal: 2,
        ref_id: 'bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb',
        ref_kind: 'finding',
        title: 'Grain export sub-claim',
      },
    ],
  },
}

describe('extractCitations', () => {
  it('reads the nested envelope path (body.data.citations) — the live unit shape', () => {
    const cites = extractCitations(LIVE_BODY)
    expect(cites).toHaveLength(2)
    expect(cites[0]).toEqual({
      marker: '[8]',
      refId: '50420791-b662-420b-8014-43e894c98b93',
      refKind: 'signal',
      signalId: '50420791-b662-420b-8014-43e894c98b93',
      title: 'Extreme Heat Warning, NWS Cleveland OH',
      source: undefined,
    })
    expect(cites[1].source).toContain('aljazeera')
    expect(cites[1].refKind).toBe('signal')
  })

  it('reads a composition finding-ref (ref_id + ref_kind + [[ref:N]] marker)', () => {
    const cites = extractCitations(COMPOSITION_BODY)
    expect(cites).toHaveLength(2)
    expect(cites[0]).toEqual({
      marker: '[[ref:1]]',
      refId: 'aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa',
      refKind: 'finding',
      signalId: 'aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa',
      title: 'Gulf tension sub-claim',
      source: undefined,
      // P4/S7-T3 hover-card fields — captured only when the payload carries them.
      evidenceText: 'Naval movements observed near the strait.',
      effectiveConfidence: 0.62,
      derivedFrom: ['aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa'],
    })
    // The second composition citation carries none of the hover-card fields, so
    // they stay ABSENT (never fabricated to 0 / '').
    expect(cites[1].evidenceText).toBeUndefined()
    expect(cites[1].effectiveConfidence).toBeUndefined()
    expect(cites[1].derivedFrom).toBeUndefined()
    expect(cites[1].refKind).toBe('finding')
    expect(cites[1].refId).toBe('bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb')
  })

  it('back-compat: a legacy composition row (only signal_id, no ref_kind) reads as a signal ref', () => {
    const cites = extractCitations({
      data: {
        citations: [{ marker: '[[ref:5]]', signal_id: 'cccccccc-3333-4333-8333-cccccccccccc' }],
      },
    })
    expect(cites).toEqual([
      {
        marker: '[[ref:5]]',
        refId: 'cccccccc-3333-4333-8333-cccccccccccc',
        refKind: 'signal',
        signalId: 'cccccccc-3333-4333-8333-cccccccccccc',
        title: undefined,
        source: undefined,
      },
    ])
  })

  it('falls back to a top-level citations list', () => {
    const cites = extractCitations({
      citations: [{ marker: '3', signal_id: 'sig-3' }],
    })
    expect(cites).toEqual([
      { marker: '[3]', refId: 'sig-3', refKind: 'signal', signalId: 'sig-3', title: undefined, source: undefined },
    ])
  })

  it('returns [] for an uncited / legacy finding (honest degrade, no throw)', () => {
    expect(extractCitations({ body: 'legacy prose, no citations' })).toEqual([])
    expect(extractCitations({ data: { category: 'x' } })).toEqual([])
    expect(extractCitations(null)).toEqual([])
    expect(extractCitations(undefined)).toEqual([])
  })

  it('skips malformed citation entries (missing marker or id)', () => {
    const cites = extractCitations({
      data: {
        citations: [
          { marker: '[1]' }, // no id
          { signal_id: 'sig-x' }, // no marker
          { marker: '[[ref:9]]', ref_kind: 'finding' }, // no ref_id
          'not-an-object',
          { marker: '[2]', signal_id: 'sig-2' }, // valid
        ],
      },
    })
    expect(cites).toEqual([
      { marker: '[2]', refId: 'sig-2', refKind: 'signal', signalId: 'sig-2', title: undefined, source: undefined },
    ])
  })
})

describe('normalizeMarker', () => {
  it('wraps a bare number and keeps bracketed markers (unit + composition)', () => {
    expect(normalizeMarker('8')).toBe('[8]')
    expect(normalizeMarker('[8]')).toBe('[8]')
    expect(normalizeMarker('[[ref:3]]')).toBe('[[ref:3]]')
    expect(normalizeMarker('')).toBeUndefined()
    expect(normalizeMarker(null)).toBeUndefined()
  })
})

describe('citationsByMarker + evidenceAnchorId', () => {
  it('indexes by marker and builds a stable anchor id', () => {
    const byMarker = citationsByMarker(extractCitations(LIVE_BODY))
    expect(byMarker.get('[8]')?.refId).toBe('50420791-b662-420b-8014-43e894c98b93')
    expect(evidenceAnchorId('sig-9')).toBe('evidence-sig-9')
  })
})

describe('splitProse', () => {
  const unitByMarker = citationsByMarker(extractCitations(LIVE_BODY))
  const compByMarker = citationsByMarker(extractCitations(COMPOSITION_BODY))

  it('tokenizes unit prose into text + resolved marker chips', () => {
    const tokens = splitProse('A heatwave [8] strains the grid [10].', unitByMarker)
    expect(tokens.map((t) => t.kind)).toEqual(['text', 'marker', 'text', 'marker', 'text'])
    const markerTokens = tokens.filter((t) => t.kind === 'marker')
    expect(markerTokens).toHaveLength(2)
    if (markerTokens[0].kind === 'marker') {
      expect(markerTokens[0].citation.refId).toBe('50420791-b662-420b-8014-43e894c98b93')
      expect(markerTokens[0].citation.refKind).toBe('signal')
    }
  })

  it('tokenizes composition prose on [[ref:N]] markers → finding refs', () => {
    const tokens = splitProse('Gulf tension [[ref:1]] and grain [[ref:2]] both rise.', compByMarker)
    const markerTokens = tokens.filter((t) => t.kind === 'marker')
    expect(markerTokens.map((t) => (t.kind === 'marker' ? t.marker : ''))).toEqual([
      '[[ref:1]]',
      '[[ref:2]]',
    ])
    if (markerTokens[0].kind === 'marker') {
      expect(markerTokens[0].citation.refKind).toBe('finding')
      expect(markerTokens[0].citation.refId).toBe('aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa')
    }
  })

  it('the combined regex is disjoint: a mixed body tokenizes each form once', () => {
    const merged = citationsByMarker([
      ...extractCitations(LIVE_BODY),
      ...extractCitations(COMPOSITION_BODY),
    ])
    const tokens = splitProse('a [[ref:1]] b [8] c', merged)
    const markers = tokens.filter((t) => t.kind === 'marker').map((t) => (t.kind === 'marker' ? t.marker : ''))
    expect(markers).toEqual(['[[ref:1]]', '[8]'])
  })

  it('leaves an unknown marker (either form) embedded as literal text', () => {
    const tokens = splitProse('A claim [99] and [[ref:42]] with no citation.', unitByMarker)
    expect(tokens).toEqual([{ kind: 'text', text: 'A claim [99] and [[ref:42]] with no citation.' }])
  })

  it('returns a single text token for prose with no markers', () => {
    const tokens = splitProse('plain prose', unitByMarker)
    expect(tokens).toEqual([{ kind: 'text', text: 'plain prose' }])
  })
})
