/**
 * printDocument (7b-iii, lane k1) — the print / save-as-PDF document for a
 * composed collection export.
 *
 * `POST /api/v1/v3/export` composes the document server-side and
 * `export_api.py`'s own docstring fixes the division of labour: *"print-PDF
 * stays a client-side browser print of the markdown view"*. This module is
 * that client side, and it does the job in a DEDICATED document rather than
 * by print-styling the workstation: {@link buildPrintDocument} turns the
 * returned markdown into one self-contained, inline-styled HTML file, and
 * {@link printExportDocument} hands it to a hidden, off-screen iframe and
 * calls that frame's `print()`. Printing a frame prints the frame's document
 * alone, so no shell chrome, no Dockview tab strip and no panel body can
 * reach the page — and no route is added, which the workstation's shell
 * forbids anyway.
 *
 * **Nothing is reformatted.** Every figure, label and verify state the
 * markdown carries is printed with the characters the composer wrote. The
 * document is only RE-LAID-OUT in two ways, both additive:
 *
 *   * each item section's `### Citations` block is lifted to a numbered
 *     **endnote** at the end (grouped under its own section heading so the
 *     body's `[N]` / `[[ref:N]]` markers still resolve, and numbered
 *     continuously in citation order across the document), and
 *   * the appendix — the Desk Brief's open-situations block, which
 *     `render_markdown` prints after every basket item — starts a new page,
 *     and so does the server-composed TYPED ABSENCE section (k5b) that
 *     follows it, split out on its own heading so the two do not run
 *     together: what a desk HAS and what it does not are two sections.
 *
 * **What an endnote can honestly carry.**
 *   * **masthead** is the cited URL's own host (`www.` stripped) — the only
 *     publisher identity the exported line carries, printed beside the URL it
 *     was read from, never mapped to a brand name;
 *   * **date** prints only when the exported citation line states one, and it
 *     prints the route's own LABEL with it. Since wave O / lane o2 the route
 *     dates every signal endnote it can: `published <date>` when the source
 *     declared one, `fetched <date>` when it did not and the platform's read
 *     stamp is all there is. The label is carried through verbatim, never
 *     dropped and never normalised into a single word, because "published
 *     2026-09-04" and "fetched 2026-09-04" are different claims and a printed
 *     page has no tooltip to explain which one the reader is holding. A date
 *     is still never guessed from the headline or the URL path, and a citation
 *     the route could not date renders as an explicit absence.
 * An endnote with none of the three says so in words rather than printing an
 * empty meta line.
 *
 * Everything but {@link printExportDocument} is pure and DOM-free, so the
 * document assembly is unit-tested without a browser.
 */
import { esc, miniMarkdownToHtml } from '@/lib/reportDownload'

/** One citation lifted out of an item section's `### Citations` block. */
export interface PrintEndnote {
  /** The marker exactly as the export wrote it (`[3]`, `[[ref:2]]`). */
  marker: string
  /** The citation label — the exported line's first ` — ` part, verbatim. */
  label: string
  /** The remaining ` — ` parts, verbatim, minus the bare-URL one. */
  notes: string[]
  /** The cited URL's host, `www.` stripped; null when the line carries none. */
  masthead: string | null
  /** A date the exported line states; null when it states none. */
  date: string | null
  /**
   * The word the export route put in FRONT of that date — `published` or
   * `fetched` — or null for a line that states a bare date. It is printed with
   * the date, because which date it is changes what the endnote means.
   */
  dateLabel: string | null
  /** The cited URL; null when the line carries none. */
  url: string | null
  /** Indented continuation lines (the W-3 spine hop), verbatim. */
  continuation: string[]
}

/** The endnotes of one item section, with the number its first note takes. */
export interface EndnoteGroup {
  /** The section's `## N. Title` heading text, verbatim. */
  heading: string
  /** 1-based number of this group's FIRST endnote in the document. */
  start: number
  notes: PrintEndnote[]
}

/** The export markdown split at `render_markdown`'s own `---` separators. */
export interface ExportMarkdownParts {
  /** The document header block: `# title`, the provenance note, the stamps. */
  header: string
  /** One entry per `## N. <title>` basket item, in basket order. */
  items: string[]
  /** Everything after the last item and before the absences — the `appendix`
   *  field, or null. */
  appendix: string | null
  /** The server-composed TYPED ABSENCE section (k5b), or null when the export
   *  did not ask for one. Split out from the appendix so it gets its own
   *  printed page: what a desk does not have is a section of the brief, not a
   *  paragraph at the end of the situations list. */
  absences: string | null
}

/**
 * The absences section's own heading, as `registry/export_absences.py` writes
 * it (`ABSENCES_HEADING`). The two live in two languages and cannot share a
 * literal, so `tests/data_pkg/test_export_absences.py` parses this file and
 * fails on drift in either direction — a renamed heading would silently drop
 * the section back into the appendix and lose its page break.
 */
export const ABSENCES_HEADING = 'Typed absence for this desk'

const ABSENCES_HEADING_LINE = new RegExp(
  `^##\\s+${ABSENCES_HEADING.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*$`,
)

const HR_LINE = /^-{3,}\s*$/
const ITEM_HEADING = /^##\s+\d+\.\s+(.*)$/
const CITATIONS_HEADING = /^###\s+Citations\s*$/
const ANY_HEADING = /^#{1,6}\s/
const MARKER = /^(\[\[[^\]]*\]\]|\[[^\]]*\])\s*/
const BARE_URL = /^https?:\/\/\S+$/
const DATE_IN_TEXT = /\b(\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?)/
/** The route's own dated-citation shape: `published 2026-09-04` / `fetched …`.
 *  A closed set of two labels — anything else is read as a bare date. */
const LABELLED_DATE = /\b(published|fetched)\s+(\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?)/i

/** True iff a chunk opens with a `## N. <title>` basket-item heading. */
function isItemChunk(chunk: string): boolean {
  return ITEM_HEADING.test(chunk.trim().split(/\r?\n/, 1)[0] ?? '')
}

/** Break one `---`-delimited block at each `## N. <title>` heading it holds,
 *  so a document whose composer omitted the rules still sections correctly. */
function splitAtItemHeadings(block: string): string[] {
  const chunks: string[] = []
  let cur: string[] = []
  for (const line of block.split(/\r?\n/)) {
    if (ITEM_HEADING.test(line)) {
      if (cur.some((l) => l.trim() !== '')) chunks.push(cur.join('\n'))
      cur = [line]
    } else {
      cur.push(line)
    }
  }
  chunks.push(cur.join('\n'))
  return chunks
}

/**
 * Split the composed markdown the way `export_api.render_markdown` built it:
 * a header block, one chunk per `## N. <title>` basket item, then whatever
 * follows — the `appendix`. The first chunk after the items ENDS the item run,
 * so an appendix that carries its own `---` rules stays whole.
 */
export function splitExportMarkdown(markdown: string): ExportMarkdownParts {
  const blocks: string[] = []
  let cur: string[] = []
  for (const line of (markdown ?? '').split(/\r?\n/)) {
    if (HR_LINE.test(line)) {
      blocks.push(cur.join('\n'))
      cur = []
    } else {
      cur.push(line)
    }
  }
  blocks.push(cur.join('\n'))
  const chunks = blocks.flatMap(splitAtItemHeadings).filter((c) => c.trim() !== '')

  let i = 0
  const headerParts: string[] = []
  for (; i < chunks.length && !isItemChunk(chunks[i]); i += 1) {
    headerParts.push(chunks[i].trim())
  }
  const items: string[] = []
  for (; i < chunks.length && isItemChunk(chunks[i]); i += 1) {
    items.push(chunks[i].trim())
  }
  const tail = chunks.slice(i).map((c) => c.trim())

  // k5b — the typed-absence section and everything after it leaves the
  // appendix. The server emits its own `---` before the heading, so it is
  // normally a chunk of its own; a composer that omitted the rule is still
  // handled by splitting that chunk at the heading line itself.
  const appendixParts: string[] = []
  const absenceParts: string[] = []
  for (const chunk of tail) {
    if (absenceParts.length > 0) {
      absenceParts.push(chunk)
      continue
    }
    const lines = chunk.split(/\r?\n/)
    const at = lines.findIndex((l) => ABSENCES_HEADING_LINE.test(l))
    if (at < 0) {
      appendixParts.push(chunk)
      continue
    }
    const before = lines.slice(0, at).join('\n').trim()
    if (before) appendixParts.push(before)
    absenceParts.push(lines.slice(at).join('\n').trim())
  }

  return {
    header: headerParts.join('\n\n'),
    items,
    appendix: appendixParts.length ? appendixParts.join('\n\n---\n\n') : null,
    absences: absenceParts.length ? absenceParts.join('\n\n---\n\n') : null,
  }
}

/** The `## N. <title>` heading text of an item block, verbatim. */
export function itemHeading(block: string): string {
  return ITEM_HEADING.exec(block.trim().split(/\r?\n/, 1)[0] ?? '')?.[1]?.trim() ?? ''
}

/** The cited URL's host, `www.` stripped — the only publisher identity the
 *  exported citation line carries. Null for anything that is not an http(s)
 *  URL (a relative receipt path, a desk-grounding block, a sub-claim ref). */
export function citationMasthead(url: string | null): string | null {
  if (!url) return null
  const host = /^https?:\/\/([^/?#]+)/i.exec(url)?.[1]
  return host ? host.replace(/^www\./i, '') || null : null
}

/**
 * Parse one citation bullet (its first line plus any indented continuation
 * lines) into an endnote. The line's own text is preserved part-for-part —
 * only the part that is a BARE url is lifted into {@link PrintEndnote.url},
 * so nothing is printed twice and nothing is dropped.
 */
export function parseCitationEntry(lines: string[]): PrintEndnote {
  const first = (lines[0] ?? '').replace(/^\s*-\s+/, '')
  const m = MARKER.exec(first)
  const marker = m ? m[1] : ''
  const rest = m ? first.slice(m[0].length) : first
  const parts = rest.split(' — ')
  const label = (parts[0] ?? '').trim()
  let url: string | null = null
  const notes: string[] = []
  for (const part of parts.slice(1)) {
    const trimmed = part.trim()
    if (url === null && BARE_URL.test(trimmed)) {
      url = trimmed
      continue
    }
    notes.push(trimmed)
  }
  // A date is read from the NOTES only, never from the label: a headline that
  // happens to contain a date is not a publication date, and inventing one
  // here would be the exact fabrication the export route refuses.
  const joined = notes.join(' ')
  const labelled = LABELLED_DATE.exec(joined)
  const date = labelled?.[2] ?? DATE_IN_TEXT.exec(joined)?.[1] ?? null
  const dateLabel = labelled?.[1]?.toLowerCase() ?? null
  return {
    marker,
    label,
    notes,
    masthead: citationMasthead(url),
    date,
    dateLabel,
    url,
    continuation: lines.slice(1).map((l) => l.replace(/^\s*-\s+/, '').trim()),
  }
}

/**
 * Lift an item block's `### Citations` block out: the body that remains (to be
 * printed in place) and the citations (to be printed as endnotes). A block
 * with no citations keeps the composer's own
 * `*(no citations recorded on this row)*` line in the body — the absence is
 * printed, never tidied away.
 */
export function extractCitations(block: string): {
  body: string
  entries: PrintEndnote[]
} {
  const body: string[] = []
  const raw: string[][] = []
  let inCitations = false
  for (const line of (block ?? '').split(/\r?\n/)) {
    if (CITATIONS_HEADING.test(line)) {
      inCitations = true
      continue
    }
    if (inCitations && ANY_HEADING.test(line)) inCitations = false
    if (!inCitations) {
      body.push(line)
      continue
    }
    if (/^-\s+/.test(line)) raw.push([line])
    else if (line.trim() !== '' && raw.length > 0) raw[raw.length - 1].push(line)
  }
  return {
    body: body.join('\n').replace(/\n{3,}/g, '\n\n').trim(),
    entries: raw.map(parseCitationEntry),
  }
}

/**
 * Every endnote in the document, grouped by the section it was cited in and
 * numbered continuously in citation order. A section with no citations
 * contributes no group.
 */
export function collectEndnotes(markdown: string): EndnoteGroup[] {
  const { items } = splitExportMarkdown(markdown)
  const groups: EndnoteGroup[] = []
  let next = 1
  for (const block of items) {
    const { entries } = extractCitations(block)
    if (entries.length === 0) continue
    groups.push({ heading: itemHeading(block), start: next, notes: entries })
    next += entries.length
  }
  return groups
}

/** The document's own `# ` title, verbatim; null when it carries none. */
export function exportDocumentTitle(markdown: string): string | null {
  return /^#\s+(.+)$/m.exec(markdown ?? '')?.[1]?.trim() || null
}

/** The `- generated_at: …` stamp the export header carries; null when absent —
 *  the print header then states the absence rather than stamping today. */
export function exportGeneratedAt(markdown: string): string | null {
  return /^-\s+generated_at:\s*(.+)$/m.exec(markdown ?? '')?.[1]?.trim() || null
}

export interface PrintDocumentOptions {
  /** The composed export markdown, exactly as the route returned it. */
  markdown: string
  /** Title override; the markdown's own `# ` heading wins when absent. */
  title?: string | null
  /** The filename the export carried — printed in the colophon. */
  filename?: string | null
  /** When this print was taken. Injected so the builder stays pure. */
  printedAt?: string | null
}

/** A4/Letter page margins, a repeating running header, endnote and appendix
 *  rules. `@page` sets margins only — the paper size stays the operator's, so
 *  the same document prints correctly on A4 and on Letter. */
const PRINT_CSS = `
  @page { margin: 18mm 16mm; }
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  html, body { background: #fff; color: #111; }
  body {
    font: 10.5pt/1.5 "Iowan Old Style", Georgia, "Times New Roman", serif;
    margin: 0; padding: 13mm 0 0;
  }
  .running-header {
    position: fixed; top: 0; left: 0; right: 0;
    display: flex; gap: 8px; justify-content: space-between; align-items: baseline;
    font: 8pt/1.3 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    color: #555; background: #fff;
    border-bottom: 0.4pt solid #bbb; padding-bottom: 3px;
  }
  .running-header .doc-title { font-weight: 600; color: #222; }
  .running-header .as-of { white-space: nowrap; }
  .running-header .as-of.absent { font-style: italic; color: #777; }
  h1 { font-size: 17pt; line-height: 1.25; margin: 0 0 6px; }
  h2 { font-size: 12.5pt; margin: 16px 0 6px; padding-bottom: 2px;
       border-bottom: 0.4pt solid #ddd; break-after: avoid; page-break-after: avoid; }
  h3 { font-size: 10.5pt; margin: 12px 0 4px; break-after: avoid; page-break-after: avoid; }
  p { margin: 0 0 8px; orphans: 3; widows: 3; }
  ul, ol { margin: 0 0 8px 20px; padding: 0; }
  li { margin: 1px 0; }
  blockquote { margin: 0 0 10px; padding-left: 10px; border-left: 2pt solid #ddd;
               color: #444; font-style: italic; }
  code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: .9em; }
  hr { border: 0; border-top: 0.4pt solid #ddd; margin: 14px 0; }
  .doc-header { margin-bottom: 4px; }
  .doc-header ul { list-style: none; margin-left: 0; }
  .item { break-inside: auto; }
  .appendix, .absences, .endnotes { break-before: page; page-break-before: always; }
  .appendix h2, .absences h2, .endnotes h2 { margin-top: 0; }
  .absences h3 { margin-top: 10px; }
  .absences li { break-inside: avoid; page-break-inside: avoid; }
  .endnote-note { font-size: 8.5pt; color: #666; font-style: italic; }
  .endnote-list { margin-left: 24px; }
  .endnote-list li { margin: 3px 0; break-inside: avoid; page-break-inside: avoid; }
  .endnote .mk { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
                 font-size: .85em; color: #444; }
  .endnote .note { color: #444; }
  .endnote-meta { display: block; font-size: 8.5pt; color: #555; }
  .endnote-meta.absent { font-style: italic; color: #777; }
  .endnote-meta .url { word-break: break-all; }
  .endnote-sub { margin: 2px 0 0 14px; font-size: 9pt; color: #444; }
  .colophon { margin-top: 16px; padding-top: 6px; border-top: 0.4pt solid #ddd;
              font-size: 8pt; color: #777; }
  @media screen {
    body { max-width: 190mm; margin: 0 auto; padding: 13mm 16px 40px; }
  }
`

/** The one sentence that states what an endnote can and cannot carry. */
const ENDNOTE_NOTE =
  'Citations as composed by the export route, lifted here in citation order. ' +
  'Masthead is the cited URL’s own host. A date prints only where the exported ' +
  'citation states one, with the route’s own word for it: “published” is the ' +
  'source’s own date, “fetched” is when this platform read the page.'

function endnoteMetaHtml(note: PrintEndnote): string {
  const bits: string[] = []
  if (note.masthead) bits.push(`<span class="masthead">${esc(note.masthead)}</span>`)
  if (note.date) {
    // The label rides WITH the date, never instead of it and never dropped:
    // "fetched 2026-09-04" is an upper bound on the publication and a reader
    // holding the printed page has no other way to know that.
    const shown = note.dateLabel ? `${note.dateLabel} ${note.date}` : note.date
    bits.push(`<span class="date">${esc(shown)}</span>`)
  }
  if (note.url) bits.push(`<span class="url">${esc(note.url)}</span>`)
  if (bits.length === 0) {
    return (
      '<span class="endnote-meta absent">no masthead, date or URL recorded ' +
      'for this citation</span>'
    )
  }
  return `<span class="endnote-meta">${bits.join(' · ')}</span>`
}

function endnoteHtml(note: PrintEndnote): string {
  const head = [
    note.marker ? `<span class="mk">${esc(note.marker)}</span>` : '',
    `<span class="label">${esc(note.label)}</span>`,
  ]
    .filter(Boolean)
    .join(' ')
  const notes = note.notes.length
    ? ` <span class="note">— ${note.notes.map(esc).join(' — ')}</span>`
    : ''
  const sub = note.continuation.length
    ? `<ul class="endnote-sub">${note.continuation
        .map((c) => `<li>${esc(c)}</li>`)
        .join('')}</ul>`
    : ''
  return `<li class="endnote">${head}${notes}${endnoteMetaHtml(note)}${sub}</li>`
}

/**
 * Build the whole self-contained print document: inline CSS only, no remote
 * asset and no script, so it is CSP-safe and prints identically offline.
 */
export function buildPrintDocument(opts: PrintDocumentOptions): string {
  const markdown = opts.markdown ?? ''
  const { header, items, appendix, absences } = splitExportMarkdown(markdown)
  const title = (opts.title || exportDocumentTitle(markdown) || 'Legba export').trim()
  const asOf = exportGeneratedAt(markdown)
  const groups = collectEndnotes(markdown)

  const runningAsOf = asOf
    ? `<span class="as-of">as of ${esc(asOf)}</span>`
    : '<span class="as-of absent">as-of not stated by this export</span>'

  const body = items
    .map((block) => {
      const { body: kept } = extractCitations(block)
      return `<section class="item">${miniMarkdownToHtml(kept)}</section>`
    })
    .join('\n')

  const appendixHtml = appendix
    ? `<section class="appendix">${miniMarkdownToHtml(appendix)}</section>`
    : ''

  // k5b — its OWN page. What the desk does not have is a section of the brief,
  // and a reader who turns to it should find it whole rather than running on
  // from the last tracked event.
  const absencesHtml = absences
    ? `<section class="absences">${miniMarkdownToHtml(absences)}</section>`
    : ''

  const endnotesHtml = groups.length
    ? `<section class="endnotes"><h2>Endnotes</h2>` +
      `<p class="endnote-note">${esc(ENDNOTE_NOTE)}</p>` +
      groups
        .map(
          (g) =>
            `<h3>${esc(g.heading)}</h3>` +
            `<ol class="endnote-list" start="${g.start}">` +
            g.notes.map(endnoteHtml).join('') +
            '</ol>',
        )
        .join('') +
      '</section>'
    : ''

  const colophon = [
    opts.filename ? `source document: ${esc(opts.filename)}` : '',
    opts.printedAt ? `printed ${esc(opts.printedAt)}` : '',
    groups.length === 0 ? 'no citations were recorded on the exported rows' : '',
  ]
    .filter(Boolean)
    .join(' · ')

  return (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">' +
    '<meta name="viewport" content="width=device-width, initial-scale=1">' +
    `<title>${esc(title)}</title><style>${PRINT_CSS}</style></head><body>` +
    `<div class="running-header"><span class="doc-title">${esc(title)}</span>` +
    `${runningAsOf}</div>` +
    `<header class="doc-header">${miniMarkdownToHtml(header)}</header>` +
    body +
    appendixHtml +
    absencesHtml +
    endnotesHtml +
    (colophon ? `<footer class="colophon">${colophon}</footer>` : '') +
    '</body></html>'
  )
}

/** Marks the throwaway print frame so a caller (and a test) can find it. */
export const PRINT_FRAME_ATTR = 'data-legba-print'

/** How long the frame stays in the DOM after `print()` returns, so the
 *  browser's own print pipeline still has a live document to read. */
const FRAME_TEARDOWN_MS = 1_000

/**
 * Print a built document. It goes into a hidden, OFF-SCREEN iframe (laid out
 * at page width so pagination is real, positioned outside the viewport rather
 * than sized to zero) and that frame's own `print()` is called — which prints
 * the frame's document and nothing else, so no workstation chrome reaches the
 * page. Returns false when the frame could not be opened, so the caller can
 * say so instead of silently doing nothing.
 */
export function printExportDocument(opts: PrintDocumentOptions): boolean {
  const html = buildPrintDocument(opts)
  // Keyboard reachability: the frame takes focus to print, so remember where
  // the operator was and put them back when the frame goes.
  const returnFocusTo = document.activeElement as HTMLElement | null
  const frame = document.createElement('iframe')
  frame.setAttribute(PRINT_FRAME_ATTR, 'true')
  frame.setAttribute('aria-hidden', 'true')
  frame.setAttribute('tabindex', '-1')
  frame.setAttribute('title', 'print document')
  frame.style.cssText =
    'position:fixed;left:-10000px;top:0;width:210mm;height:297mm;border:0;'
  document.body.appendChild(frame)
  const doc = frame.contentDocument
  if (!doc) {
    frame.remove()
    return false
  }
  doc.open()
  doc.write(html)
  doc.close()
  const run = () => {
    try {
      frame.contentWindow?.focus()
      frame.contentWindow?.print()
    } catch {
      // A browser that refuses the print dialog leaves the document alone;
      // the operator still has the downloaded markdown.
    }
    window.setTimeout(() => {
      frame.remove()
      if (returnFocusTo?.isConnected) returnFocusTo.focus()
    }, FRAME_TEARDOWN_MS)
  }
  if (doc.readyState === 'complete') window.setTimeout(run, 0)
  else frame.addEventListener('load', run, { once: true })
  return true
}
