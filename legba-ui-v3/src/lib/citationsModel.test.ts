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
  citationLabel,
  evidenceAnchorId,
  normalizeMarker,
  splitProse,
  tokenizeProse,
  citationAnchorId,
  citationDrill,
  citationKindLabel,
  isGroundingCitation,
  MARKER_CLASS_GROUNDING,
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

describe('citationLabel', () => {
  it('collapses both marker forms to the clean bracketed ordinal [N]', () => {
    // A composition ordinal marker renders the same clean short form as a unit
    // marker — one consistent chip label everywhere (S7-T4 fix).
    expect(citationLabel('[[ref:3]]')).toBe('[3]')
    expect(citationLabel('[8]')).toBe('[8]')
    expect(citationLabel('[[ref:12]]')).toBe('[12]')
  })

  it('falls back to the raw marker when no ordinal is extractable (never fabricated)', () => {
    expect(citationLabel('[abc]')).toBe('[abc]')
    expect(citationLabel('')).toBe('')
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

// ---------------------------------------------------------------------------
// DESK GROUNDING blocks (2026-08-30 consumer repair).
//
// Five block kinds; the model handled exactly ONE (`situation_register`), and
// this file had coverage for NONE of them — not even the one it handled — so
// nothing guarded the gap when FRAME-2's `window_ledger` landed after
// `situation_register` had been special-cased.
// ---------------------------------------------------------------------------

const PRIOR_READ_ID = 'eeeeeeee-5555-4555-8555-eeeeeeeeeeee'

/** One entry per grounding kind, in the shape `citation_for_block` writes. */
const GROUNDING_BODY = {
  body: 'Prior [1], ledger [2], register [3], baseline [4], questions [5].',
  data: {
    citations: [
      {
        marker: '[1]',
        ref_kind: 'prior_read',
        ref_id: PRIOR_READ_ID,
        grounding: 'prior_read',
        title: 'Ruritania — morning read',
        evidence_text: 'BLUF: flat.',
        marker_class: 'desk_grounding',
        resolves_against: 'data.citations',
      },
      {
        marker: '[2]',
        ref_kind: 'window_ledger',
        grounding: 'window_ledger',
        title: 'Window ledger',
        evidence_text: '16 July — elevated',
        marker_class: 'desk_grounding',
        resolves_against: 'data.citations',
      },
      {
        marker: '[3]',
        ref_kind: 'situation_register',
        grounding: 'situation_register',
        evidence_text: 'open frame — active',
        marker_class: 'desk_grounding',
        resolves_against: 'data.citations',
      },
      {
        marker: '[4]',
        ref_kind: 'desk_baseline',
        grounding: 'desk_baseline',
        title: 'Desk baseline',
        evidence_text: 'expected 41.2',
        marker_class: 'desk_grounding',
        resolves_against: 'data.citations',
      },
      {
        marker: '[5]',
        ref_kind: 'open_questions',
        grounding: 'open_questions',
        title: 'Standing open questions',
        evidence_text: 'Who holds Route 9?',
        marker_class: 'desk_grounding',
        resolves_against: 'data.citations',
      },
    ],
  },
}

/** The same five without the structural marks — every row written before the
 *  `2026-08-30/1` stamp. */
const PRE_STAMP_GROUNDING = {
  body: GROUNDING_BODY.body,
  data: {
    citations: GROUNDING_BODY.data.citations.map(
      ({ marker_class: _mc, resolves_against: _ra, ...rest }) => rest,
    ),
  },
}

describe('extractCitations — desk grounding blocks', () => {
  it('keeps ALL FIVE kinds, the id-less ones included', () => {
    const cites = extractCitations(GROUNDING_BODY)
    expect(cites).toHaveLength(5)
    expect(cites.map((c) => c.refKind)).toEqual([
      'prior_read',
      'window_ledger',
      'situation_register',
      'desk_baseline',
      'open_questions',
    ])
  })

  it('carries the structural marks through instead of re-deriving them', () => {
    for (const c of extractCitations(GROUNDING_BODY)) {
      expect(c.markerClass).toBe(MARKER_CLASS_GROUNDING)
      expect(c.resolvesAgainst).toBe('data.citations')
      expect(isGroundingCitation(c)).toBe(true)
    }
  })

  it('resolves a PRE-STAMP row (no marker_class) off the registered ref_kind', () => {
    const cites = extractCitations(PRE_STAMP_GROUNDING)
    expect(cites).toHaveLength(5)
    expect(cites.every((c) => isGroundingCitation(c))).toBe(true)
    // Nothing is invented for a row that never carried the mark.
    expect(cites.every((c) => c.markerClass === undefined)).toBe(true)
    expect(cites.every((c) => c.resolvesAgainst === undefined)).toBe(true)
  })

  it('the prior read drills to a FINDING; the other four drill nowhere', () => {
    const [prior, ...synthetic] = extractCitations(GROUNDING_BODY)
    expect(citationDrill(prior)).toEqual({ kind: 'finding', id: PRIOR_READ_ID })
    // It used to fall through to refKind 'signal' — a chip labelled "signal"
    // drilling to a signal row that has never existed.
    expect(prior.refKind).not.toBe('signal')
    expect(prior.signalId).toBe('')
    for (const c of synthetic) {
      expect(citationDrill(c)).toBeNull()
      expect(c.refId).toBe('')
    }
  })

  it('every kind has an honest label — none reads "Unresolved"', () => {
    expect(extractCitations(GROUNDING_BODY).map(citationKindLabel)).toEqual([
      'prior read',
      'window ledger',
      'situation register',
      'desk baseline',
      'open questions',
    ])
  })

  it('falls back to a per-kind title when the entry carries none', () => {
    expect(extractCitations(GROUNDING_BODY)[2].title).toBe('Open-situation register')
  })

  it('every grounding marker resolves in the tokenizer (no unresolved token)', () => {
    const byMarker = citationsByMarker(extractCitations(GROUNDING_BODY))
    const tokens = tokenizeProse(GROUNDING_BODY.body, byMarker)
    expect(tokens.filter((t) => t.kind === 'unresolved')).toHaveLength(0)
    expect(tokens.filter((t) => t.kind === 'marker')).toHaveLength(5)
  })

  it('id-less blocks get DISTINCT anchors (a shared `evidence-` id collided)', () => {
    const cites = extractCitations(GROUNDING_BODY)
    const anchors = cites.map(citationAnchorId)
    expect(new Set(anchors).size).toBe(5)
    expect(anchors[0]).toBe(`evidence-${PRIOR_READ_ID}`)
  })

  it('a SIXTH kind this bundle predates is carried verbatim, never guessed', () => {
    // Marked as grounding, `ref_kind` not in the registry, and carrying an id.
    // Defaulting it to `prior_read` would render a drill link to that id —
    // `prior_read` is the ONE grounding kind that drills, so it is the worst
    // possible guess. It must be labeled by its own kind and drill nowhere.
    const [c] = extractCitations({
      body: 'A claim [1].',
      data: {
        citations: [{
          marker: '[1]',
          ref_kind: 'future_block',
          ref_id: 'ffffffff-6666-4666-8666-ffffffffffff',
          marker_class: 'desk_grounding',
          resolves_against: 'data.citations',
          evidence_text: 'something new',
        }],
      },
    })
    expect(isGroundingCitation(c)).toBe(true)
    expect(c.refKind).toBe('future_block')
    expect(citationKindLabel(c)).toBe('future_block')
    expect(citationDrill(c)).toBeNull()
    expect(c.refId).toBe('')
    expect(c.title).toBe('Desk grounding block')
  })

  it('signal and sub-claim refs are untouched by the grounding branch', () => {
    for (const c of extractCitations(LIVE_BODY)) {
      expect(isGroundingCitation(c)).toBe(false)
      expect(citationKindLabel(c)).toBe('signal')
      expect(citationDrill(c)).toEqual({ kind: 'signal', id: c.refId })
    }
    for (const c of extractCitations(COMPOSITION_BODY)) {
      expect(isGroundingCitation(c)).toBe(false)
      expect(citationKindLabel(c)).toBe('sub-claim')
      expect(citationDrill(c)).toEqual({ kind: 'finding', id: c.refId })
    }
  })
})
