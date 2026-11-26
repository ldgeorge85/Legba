/**
 * Claim text on the Board's list: one sentence, with the markup the writers
 * leave in it handled, so no raw marker reaches the screen.
 *
 * What happens to each marker:
 *
 *   `**x**`, `__x__`         RENDERED: bold
 *   `*x*`                    RENDERED: italic (a lone `*` is left alone)
 *   `[text](url)`            RENDERED as its text (the row opens the record)
 *   `` `x` ``                RENDERED as plain text
 *   a leading `BLUF:` label  STRIPPED: a writer's label, not part of the claim
 *   `[[ref:1]]`, `[[ref:1, 2]]`, `[1]`, `[1, 2]`, `【1】`
 *                            STRIPPED: citation markers; the citations are on
 *                            the record the row opens
 *   a leading `> ` (quote) or `- ` / `* ` / `• ` (list) on any line
 *                            STRIPPED: the row is already a quotation
 *   a leading `#` (heading)  STRIPPED
 *   line breaks              folded into spaces
 *
 * Identifiers in the text (`military_posture`, a subject id) are said in
 * words: a subject by its declared name, anything else with its underscores
 * read as spaces.
 */
import type { ReactNode } from 'react'
import { subjectName } from '@/lib/deskNames'

/** Longest sentence the list prints before it cuts at a word. */
export const SENTENCE_MAX = 200

const REF_MARKERS = /\[\[\s*ref\s*:[^\]]*\]\]|\[\s*\d+(?:\s*[,–-]\s*\d+)*\s*\]|【\s*\d+(?:\s*[,–-]\s*\d+)*\s*】/g
const LINE_LEADERS = /^\s*(?:>\s?)+|^\s*(?:[-*•]\s+)|^\s*#{1,6}\s+/
const BLUF = /^\s*(?:\*\*|__)?\s*BLUF\s*:?\s*(?:\*\*|__)?\s*:?\s*/i
const SNAKE = /\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b/g

/** Identifiers said in words. */
export function plainIds(text: string): string {
  return text.replace(SNAKE, (id) => subjectName(id) ?? id.replace(/_/g, ' '))
}

/** The text with every structural marker stripped (bold and italic marks kept). */
export function claimSource(text: string | null | undefined): string {
  if (!text) return ''
  const lines = text
    .split(/\r?\n/)
    .map((l) => {
      let line = l
      // leaders can stack ("> - x"): strip until none is left
      for (let i = 0; i < 4 && LINE_LEADERS.test(line); i++) line = line.replace(LINE_LEADERS, '')
      return line
    })
    .filter((l) => l.trim() !== '')
  let out = lines.join(' ')
  out = out.replace(BLUF, '')
  out = out.replace(REF_MARKERS, '')
  out = out.replace(/\[([^\]]+)\]\((?:[^)]*)\)/g, '$1')
  out = out.replace(/`([^`]*)`/g, '$1')
  out = plainIds(out)
  // a marker stripped before a stop leaves " ." / " ,": close the gap
  out = out.replace(/\s+([.,;:!?)])/g, '$1').replace(/\(\s*\)/g, '')
  // ...and a run of stops where markers stood (": , .") keeps only its last
  out = out.replace(/[,;:](?=[,;:.!?])/g, '')
  return out.replace(/\s{2,}/g, ' ').trim()
}

/** One sentence of it: the first sentence when the text runs long, else the text, cut at a word. */
export function oneSentence(text: string, max = SENTENCE_MAX): string {
  if (text.length <= max) return text
  const m = /^(.{40,}?[.!?])(?:["”’)]*)\s/.exec(text)
  if (m && m[1].length <= max) return m[1]
  const cut = text.slice(0, max)
  const at = cut.lastIndexOf(' ')
  return `${(at > 40 ? cut.slice(0, at) : cut).replace(/[\s,;:]+$/, '')}…`
}

type Seg = { text: string; strong?: boolean; em?: boolean }

/** The bold and italic runs of a stripped text. */
export function claimSegments(source: string): Seg[] {
  const out: Seg[] = []
  const re = /(\*\*|__)(.+?)\1|\*(?!\s)([^*]+?)\*(?!\*)/g
  let last = 0
  let m: RegExpExecArray | null
  while ((m = re.exec(source))) {
    if (m.index > last) out.push({ text: source.slice(last, m.index) })
    if (m[2] !== undefined) out.push({ text: m[2], strong: true })
    else out.push({ text: m[3], em: true })
    last = m.index + m[0].length
  }
  if (last < source.length) out.push({ text: source.slice(last) })
  // an unmatched mark left over is a stray character, never markup on screen
  return out.map((s) => ({ ...s, text: s.text.replace(/\*\*|__/g, '') })).filter((s) => s.text !== '')
}

/** The text a screen reader or a title gets: no marks at all. */
export function claimPlain(text: string | null | undefined, max = SENTENCE_MAX): string {
  return claimSegments(oneSentence(claimSource(text), max))
    .map((s) => s.text)
    .join('')
}

/** One sentence of claim text, its bold and italic rendered, every other marker stripped. */
export function ClaimText({ text, max = SENTENCE_MAX }: { text: string | null | undefined; max?: number }): ReactNode {
  const segs = claimSegments(oneSentence(claimSource(text), max))
  return segs.map((s, i) =>
    s.strong ? (
      <strong key={i} className="font-semibold">
        {s.text}
      </strong>
    ) : s.em ? (
      <em key={i}>{s.text}</em>
    ) : (
      <span key={i}>{s.text}</span>
    ),
  )
}
