/**
 * printDocument (7b-iii, lane k1) — assembly of the print / save-as-PDF
 * document for a composed export.
 *
 * The fixture is the exact shape `export_api.render_markdown` emits: a header
 * block with the provenance note and the `generated_at` stamp, `---`-ruled
 * `## N. <title>` item sections each with its own `### Citations` block, and
 * the Desk Brief `appendix` last, and — when the export asked for it — the
 * server-composed TYPED ABSENCE section (k5b) after that.
 *
 * Asserts: the running header carries the document title and the export's own
 * as-of (and SAYS SO when the export states none); citations leave the body
 * and come back as endnotes numbered continuously in citation order; the
 * masthead is the cited URL's host, a stated publication date is printed
 * with the route's own label (`published` / `fetched`, o2) and an absent
 * one is stated rather than guessed; the appendix is page-broken; every
 * figure in the
 * markdown survives verbatim; and the document carries no workstation markup.
 */
import { describe, it, expect } from 'vitest'
import {
  ABSENCES_HEADING,
  buildPrintDocument,
  citationMasthead,
  collectEndnotes,
  exportGeneratedAt,
  extractCitations,
  itemHeading,
  parseCitationEntry,
  splitExportMarkdown,
} from './printDocument'

const EXPORT_MD = `# Desk brief — Turkey

> machine-generated export; every claim carries its citations; verify states as recorded

- generated_at: 2026-09-23T06:14:02Z
- items: 2

---

## 1. Turkey — country composition

- kind: \`country_composition\`
- analyst: meta_findings_synthesizer \`v9\`
- target: country_g20_tr
- produced_at: 2026-09-23T05:00:00Z
- confidence: 0.62 (effective 0.55 after verify fold)
- verify: faithfulness=0.81 · flags: 2 soft

Escalation risk rose to 0.41 over the trailing window [1], against a baseline of 0.28 [2].

### Citations

- [1] Ankara signals troop rotation — https://www.reuters.com/world/ankara-rotation — published 2026-09-22 — evidence preserved, sha256:abc123
- [2] Prior read (this unit's previous verified read) — *desk grounding · prior read a1b2; resolves against this finding's citation record*

---

## 2. Escalation — unit read

- kind: \`unit_finding\`
- analyst: escalation \`v3\`
- target: country_g20_tr
- produced_at: 2026-09-22T18:00:00Z
- verify: unverified — no critique row

Border incidents counted 7 in 14 days [1].

### Citations

- [1] Border wire report — https://apnews.com/article/border-wire — published 2026-09-21
  - via block 3 of that record → escalation / country_g20_tr — /api/v1/lineage/finding/f9

---

## Open situations & tracked events

### Border friction

- status: monitoring · category: security · intensity: 0.44 · situation id: s1
- tracked events (2):
  - Convoy movement reported — confirmed (event id: e1)
  - Airspace closure — suspected (event id: e2)
`

describe('splitExportMarkdown', () => {
  it('splits the header, the numbered items and the appendix', () => {
    const parts = splitExportMarkdown(EXPORT_MD)
    expect(parts.header).toContain('# Desk brief — Turkey')
    expect(parts.header).toContain('- generated_at: 2026-09-23T06:14:02Z')
    expect(parts.items).toHaveLength(2)
    expect(itemHeading(parts.items[0])).toBe('Turkey — country composition')
    expect(itemHeading(parts.items[1])).toBe('Escalation — unit read')
    expect(parts.appendix).toContain('## Open situations & tracked events')
  })

  it('still sections a document whose composer wrote no --- rules', () => {
    const parts = splitExportMarkdown('# T\n\n- items: 1\n\n## 1. Only\n\nbody.\n')
    expect(parts.items).toHaveLength(1)
    expect(itemHeading(parts.items[0])).toBe('Only')
    expect(parts.appendix).toBeNull()
  })

  it('has no appendix when the export carried none', () => {
    const parts = splitExportMarkdown('# T\n\n---\n\n## 1. A\n\nbody.\n')
    expect(parts.appendix).toBeNull()
  })
})

describe('extractCitations', () => {
  it('lifts the Citations block out of the body and keeps everything else', () => {
    const { body, entries } = extractCitations(splitExportMarkdown(EXPORT_MD).items[0])
    expect(body).toContain('Escalation risk rose to 0.41')
    expect(body).toContain('- verify: faithfulness=0.81 · flags: 2 soft')
    expect(body).not.toContain('### Citations')
    expect(body).not.toContain('reuters.com')
    expect(entries).toHaveLength(2)
  })

  it('leaves the composer’s own no-citations line in the body', () => {
    const { body, entries } = extractCitations(
      '## 1. Uncited\n\nbody.\n\n*(no citations recorded on this row)*\n',
    )
    expect(entries).toHaveLength(0)
    expect(body).toContain('*(no citations recorded on this row)*')
  })
})

describe('parseCitationEntry', () => {
  it('reads the marker, label, url and masthead off a signal citation', () => {
    const note = parseCitationEntry([
      '- [1] Ankara signals troop rotation — https://www.reuters.com/world/x — evidence preserved, sha256:abc123',
    ])
    expect(note.marker).toBe('[1]')
    expect(note.label).toBe('Ankara signals troop rotation')
    expect(note.url).toBe('https://www.reuters.com/world/x')
    expect(note.masthead).toBe('reuters.com')
    expect(note.notes).toEqual(['evidence preserved, sha256:abc123'])
  })

  it('states an absent date rather than reading one out of the headline', () => {
    const note = parseCitationEntry(['- [1] What happened on 2026-01-05 — https://x.test/a'])
    expect(note.date).toBeNull()
    expect(note.dateLabel).toBeNull()
    const dated = parseCitationEntry(['- [1] Report — https://x.test/a — published 2026-09-21'])
    expect(dated.date).toBe('2026-09-21')
  })

  it("keeps the route's own word for WHICH date the endnote carries", () => {
    // o2 — `published` is the source's own date; `fetched` is when this
    // platform read the page, which is an upper bound on the publication. A
    // printed page has no tooltip, so the word is printed.
    const published = parseCitationEntry([
      '- [1] Report — https://x.test/a — published 2026-09-21',
    ])
    expect([published.dateLabel, published.date]).toEqual(['published', '2026-09-21'])
    const fetched = parseCitationEntry([
      '- [2] Undated wire — https://x.test/b — fetched 2026-09-05',
    ])
    expect([fetched.dateLabel, fetched.date]).toEqual(['fetched', '2026-09-05'])
  })

  it('reads a bare date with no label as a bare date, not as a published one', () => {
    const note = parseCitationEntry([
      '- [1] Observation — provider · 2026-08-01 — https://x.test/o',
    ])
    expect(note.date).toBe('2026-08-01')
    expect(note.dateLabel).toBeNull()
  })

  it('never lifts a label out of the citation LABEL, only out of the notes', () => {
    const note = parseCitationEntry([
      '- [1] When it was published 2026-01-05 — https://x.test/a',
    ])
    expect(note.date).toBeNull()
    expect(note.dateLabel).toBeNull()
  })

  it('carries a desk-grounding citation with no url as an honest absence', () => {
    const note = parseCitationEntry([
      "- [2] Prior read — *desk grounding · prior read a1b2; resolves against this finding's citation record*",
    ])
    expect(note.url).toBeNull()
    expect(note.masthead).toBeNull()
    expect(note.notes[0]).toContain('desk grounding')
  })

  it('keeps an indented continuation line (the spine hop) verbatim', () => {
    const note = parseCitationEntry([
      '- [1] Border wire report — https://apnews.com/a',
      '  - via block 3 of that record → escalation / country_g20_tr',
    ])
    expect(note.continuation).toEqual([
      'via block 3 of that record → escalation / country_g20_tr',
    ])
  })

  it('reads a composition ordinal marker', () => {
    expect(parseCitationEntry(['- [[ref:4]] Sub-claim — *sub-claim finding*']).marker).toBe(
      '[[ref:4]]',
    )
  })
})

describe('citationMasthead', () => {
  it('is the host with www stripped, and null for anything that is not a URL', () => {
    expect(citationMasthead('https://www.bbc.co.uk/news/x')).toBe('bbc.co.uk')
    expect(citationMasthead('/api/v1/lineage/finding/f9')).toBeNull()
    expect(citationMasthead(null)).toBeNull()
  })
})

describe('collectEndnotes', () => {
  it('numbers endnotes continuously across sections, in citation order', () => {
    const groups = collectEndnotes(EXPORT_MD)
    expect(groups.map((g) => [g.heading, g.start, g.notes.length])).toEqual([
      ['Turkey — country composition', 1, 2],
      ['Escalation — unit read', 3, 1],
    ])
    expect(groups.flatMap((g) => g.notes.map((n) => n.marker))).toEqual([
      '[1]',
      '[2]',
      '[1]',
    ])
  })
})

describe('exportGeneratedAt', () => {
  it('reads the export stamp, and is null when the export states none', () => {
    expect(exportGeneratedAt(EXPORT_MD)).toBe('2026-09-23T06:14:02Z')
    expect(exportGeneratedAt('# T\n\n- items: 1\n')).toBeNull()
  })
})

describe('buildPrintDocument', () => {
  const html = buildPrintDocument({
    markdown: EXPORT_MD,
    filename: 'legba-export-20260923.md',
    printedAt: '2026-09-23T07:00:00Z',
  })

  it('is one self-contained document with the title in head and header', () => {
    expect(html.startsWith('<!doctype html>')).toBe(true)
    expect(html).toContain('<title>Desk brief — Turkey</title>')
    expect(html).toContain('<span class="doc-title">Desk brief — Turkey</span>')
  })

  it('carries the export’s own as-of in the running header', () => {
    expect(html).toContain('as of 2026-09-23T06:14:02Z')
  })

  it('states the absence when the export carries no as-of, never stamping one', () => {
    const noStamp = buildPrintDocument({ markdown: '# T\n\n---\n\n## 1. A\n\nbody.\n' })
    expect(noStamp).toContain('as-of not stated by this export')
    expect(noStamp).not.toMatch(/as of \d{4}-\d{2}-\d{2}/)
  })

  it('sets A4/Letter page margins and breaks the page before the appendix', () => {
    expect(html).toContain('@page { margin: 18mm 16mm; }')
    expect(html).toMatch(/\.appendix[^{]*\{[^}]*page-break-before: always/)
    expect(html).toContain('<section class="appendix">')
  })

  it('renders the citations as numbered endnotes and not in the body', () => {
    const endnotes = html.slice(html.indexOf('<section class="endnotes">'))
    const body = html.slice(0, html.indexOf('<section class="endnotes">'))
    expect(body).not.toContain('reuters.com')
    expect(endnotes).toContain('<ol class="endnote-list" start="1">')
    expect(endnotes).toContain('<ol class="endnote-list" start="3">')
    expect(endnotes).toContain('reuters.com')
    expect(endnotes).toContain('https://www.reuters.com/world/ankara-rotation')
  })

  it('says so when a citation carries no masthead, date or URL', () => {
    expect(html).toContain('no masthead, date or URL recorded for this citation')
  })

  it('prints the endnote date with its label, beside the masthead and URL', () => {
    const endnotes = html.slice(html.indexOf('<section class="endnotes">'))
    expect(endnotes).toContain('<span class="date">published 2026-09-22</span>')
    // The meta line reads masthead · date · url, in that order.
    expect(endnotes).toContain(
      '<span class="masthead">reuters.com</span> · ' +
        '<span class="date">published 2026-09-22</span> · ',
    )
  })

  it('prints every figure the markdown carries, verbatim', () => {
    for (const figure of [
      '0.41',
      '0.28',
      'faithfulness=0.81',
      'confidence: 0.62 (effective 0.55 after verify fold)',
      'unverified — no critique row',
      'intensity: 0.44',
      'sha256:abc123',
      'published 2026-09-22',
    ]) {
      expect(html).toContain(figure)
    }
  })

  it('carries no workstation chrome or test hooks into the page', () => {
    expect(html).not.toContain('data-testid')
    expect(html).not.toContain('dockview')
    expect(html).not.toContain('PanelChrome')
    expect(html).not.toContain('<script')
  })

  it('names the source document and the print time in the colophon', () => {
    expect(html).toContain('legba-export-20260923.md')
    expect(html).toContain('printed 2026-09-23T07:00:00Z')
  })

  it('escapes markup that arrives inside the composed markdown', () => {
    const hostile = buildPrintDocument({
      markdown: '# T\n\n---\n\n## 1. <img src=x onerror=alert(1)>\n\nbody.\n',
    })
    expect(hostile).not.toContain('<img src=x')
    expect(hostile).toContain('&lt;img src=x')
  })
})

// ---------------------------------------------------------------------------
// The typed-absence section (k5b) — its own page, split off the appendix
// ---------------------------------------------------------------------------

/** The section exactly as `registry/export_absences.render_absences_markdown`
 *  emits it, appended after the situations appendix with its own `---` rule. */
const ABSENCES_MD = `
---

## ${ABSENCES_HEADING}

- scope: country_g20_tr
- read_at: 2026-09-23T06:14:02Z
- typed absences recorded: 2

*Typed absence (docs/GLOSSARY.md): an absence with a scope, a kind, a proof and a shelf life, never a blank.*

### not collected

*nothing covers this subject for this desk*

- **disruption_status** — this bounded unit has never produced a read for this desk
  - no read on record for this desk
  - measured 2026-09-23T04:40:01+00:00 (the banded-scorecard run's own scan) · last known absence, NOT re-checked (was due 2026-09-23T06:00:00+00:00)
  - checked: the latest kind='finding' row for this unit (at 2026-09-23T04:40:01+00:00)
  - record of that look: \`80690b09\` (scorecard)

### searched found nothing

*the external audit searched and nothing decided the claim*

not measured for this desk: external_grades could not be read (UndefinedTableError)
`

describe('the typed-absence section', () => {
  const withAbsences = EXPORT_MD + ABSENCES_MD

  it('leaves the appendix and stands as its own part', () => {
    const parts = splitExportMarkdown(withAbsences)
    expect(parts.items).toHaveLength(2)
    // The situations block is still the appendix — and it no longer carries
    // the absences, which would have lost them their own page.
    expect(parts.appendix).toContain('## Open situations & tracked events')
    expect(parts.appendix).not.toContain(ABSENCES_HEADING)
    expect(parts.absences).toContain(`## ${ABSENCES_HEADING}`)
    expect(parts.absences).toContain('disruption_status')
    expect(parts.absences).toContain('not measured for this desk')
  })

  it('is null when the export did not ask for one — the pre-k5b document', () => {
    const parts = splitExportMarkdown(EXPORT_MD)
    expect(parts.absences).toBeNull()
    expect(parts.appendix).toContain('## Open situations & tracked events')
  })

  it('splits on the heading even when the composer wrote no --- rule', () => {
    const noRule = EXPORT_MD + ABSENCES_MD.replace('\n---\n', '\n')
    const parts = splitExportMarkdown(noRule)
    expect(parts.absences).toContain(`## ${ABSENCES_HEADING}`)
    expect(parts.appendix).toContain('## Open situations & tracked events')
    expect(parts.appendix).not.toContain(ABSENCES_HEADING)
  })

  it('prints as its own page section, after the appendix', () => {
    const html = buildPrintDocument({ markdown: withAbsences })
    expect(html).toContain('<section class="absences">')
    expect(html).toMatch(/\.appendix, \.absences[^{]*\{[^}]*page-break-before: always/)
    expect(html.indexOf('<section class="absences">')).toBeGreaterThan(
      html.indexOf('<section class="appendix">'),
    )
  })

  it('prints the stamp and the proof verbatim — nothing reformatted', () => {
    const html = buildPrintDocument({ markdown: withAbsences })
    for (const figure of [
      'last known absence, NOT re-checked',
      'was due 2026-09-23T06:00:00+00:00',
      "the latest kind='finding' row for this unit",
      '80690b09',
      'not measured for this desk: external_grades could not be read',
    ]) {
      expect(html).toContain(figure)
    }
  })
})
