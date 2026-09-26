/**
 * reportDownload — the DOM-free half: the Markdown export string and the
 * `miniMarkdownToHtml` subset renderer both print documents in this repo use.
 *
 * The renderer grew three constructs when the collection export started
 * flowing through it (7b-iii, lane k1): thematic breaks, the export header's
 * block quote, and INDENT-NESTED lists — the export writes its spine-hop and
 * tracked-event lines as sub-bullets, and flattening them would lose the
 * structure the composer chose. These are the tests for that, plus the
 * escaping guarantee the whole print path rests on.
 */
import { describe, it, expect } from 'vitest'
import { esc, miniMarkdownToHtml, reportToMarkdown } from './reportDownload'

describe('esc', () => {
  it('escapes every character that could open markup', () => {
    expect(esc('<a href="x">&</a>')).toBe('&lt;a href=&quot;x&quot;&gt;&amp;&lt;/a&gt;')
  })
})

describe('miniMarkdownToHtml', () => {
  it('renders ATX headings, paragraphs and inline emphasis', () => {
    const html = miniMarkdownToHtml('## Head\n\nsome **bold** and *italic* and `code`.')
    expect(html).toContain('<h2>Head</h2>')
    expect(html).toContain('<strong>bold</strong>')
    expect(html).toContain('<em>italic</em>')
    expect(html).toContain('<code>code</code>')
  })

  it('renders a flat bullet list', () => {
    const html = miniMarkdownToHtml('- one\n- two')
    expect(html.replace(/\n/g, '')).toBe('<ul><li>one</li><li>two</li></ul>')
  })

  it('nests an indented sub-bullet INSIDE the item that introduced it', () => {
    const html = miniMarkdownToHtml('- one\n  - hop\n- two').replace(/\n/g, '')
    expect(html).toBe('<ul><li>one<ul><li>hop</li></ul></li><li>two</li></ul>')
  })

  it('renders an ordered list', () => {
    expect(miniMarkdownToHtml('1. a\n2. b').replace(/\n/g, '')).toBe(
      '<ol><li>a</li><li>b</li></ol>',
    )
  })

  it('renders a thematic break rather than a literal --- paragraph', () => {
    const html = miniMarkdownToHtml('a\n\n---\n\nb')
    expect(html).toContain('<hr>')
    expect(html).not.toContain('<p>---</p>')
  })

  it('renders the export header’s block quote as a quote', () => {
    const html = miniMarkdownToHtml('> machine-generated export\n\nbody')
    expect(html).toContain('<blockquote>')
    expect(html).toContain('<p>machine-generated export</p>')
    expect(html).toContain('</blockquote>')
  })

  it('escapes before it formats, so markdown can never inject markup', () => {
    const html = miniMarkdownToHtml('- <img src=x onerror=alert(1)>')
    expect(html).not.toContain('<img')
    expect(html).toContain('&lt;img src=x onerror=alert(1)&gt;')
  })

  it('tolerates an empty document', () => {
    expect(miniMarkdownToHtml('')).toBe('')
  })
})

describe('reportToMarkdown', () => {
  it('carries the title, the as-of and the body, with an Evidence list', () => {
    const md = reportToMarkdown({
      title: 'Turkey read',
      body: 'Escalation at 0.41 [1].',
      producedAt: '2026-09-23T05:00:00Z',
      scope: 'country_g20_tr · country assessment',
      citations: [{ marker: '[1]', title: 'Wire report', source: 'reuters.com' } as never],
    })
    expect(md).toContain('# Turkey read')
    expect(md).toContain('as of 2026-09-23T05:00:00Z')
    expect(md).toContain('Escalation at 0.41 [1].')
    expect(md).toContain('## Evidence')
    expect(md).toContain('**[1]** — Wire report · reuters.com')
  })

  it('states an unwritten body rather than printing an empty section', () => {
    expect(reportToMarkdown({ title: 'T', body: '' })).toContain('_(no written body)_')
  })
})
