/**
 * proseText — flatten a finding/signal body into a scannable plain-text preview.
 *
 * The Live Feed scan line must NOT dump raw markdown (`**BLUF:**`, `## Key
 * points`), literal citation runs (`[3][4][31]`), or — for the finding bodies
 * that arrive as a raw `{"title":…,"body":…}` JSON string — the JSON itself.
 * These pure helpers unwrap the envelope, strip markdown to text, drop the
 * citation-marker noise, and collapse whitespace. DOM-free so they're unit
 * testable and reusable anywhere a one-line preview is needed.
 */
import { normalizeCitationMarkers } from './citationsModel'

/** Prose fields to pull out of a `{"title","body"}`-style JSON envelope, in
 *  preference order — mirrors the Inspector's REPORT_KEYS. */
const ENVELOPE_BODY_KEYS = ['body', 'summary', 'assessment', 'narrative', 'text'] as const

/**
 * If `raw` is a JSON object envelope (`{"title":…,"body":…}`), extract its prose
 * field; otherwise return `raw` unchanged. Never returns the raw JSON string —
 * an object with no prose field degrades to its title, then to empty.
 */
export function unwrapEnvelope(raw: string): string {
  const t = raw.trim()
  if (!(t.startsWith('{') && t.endsWith('}'))) return raw
  try {
    const parsed = JSON.parse(t) as Record<string, unknown>
    if (!parsed || typeof parsed !== 'object') return raw
    for (const k of ENVELOPE_BODY_KEYS) {
      const v = parsed[k]
      if (typeof v === 'string' && v.trim() !== '') return v
    }
    const title = parsed['title']
    return typeof title === 'string' ? title : ''
  } catch {
    // Not valid JSON after all — treat it as prose text.
    return raw
  }
}

/** Strip common markdown syntax to a flat, scannable run of text. */
export function stripMarkdown(md: string): string {
  return md
    .replace(/```[\s\S]*?```/g, ' ') // fenced code blocks
    .replace(/`([^`]+)`/g, '$1') // inline code
    .replace(/!\[[^\]]*\]\([^)]*\)/g, ' ') // images
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1') // links → link text
    .replace(/^\s{0,3}#{1,6}\s+/gm, '') // ATX headings
    .replace(/^\s{0,3}>\s?/gm, '') // blockquotes
    .replace(/^\s{0,3}[-*+]\s+/gm, '') // bullet lists
    .replace(/^\s{0,3}\d+[.)]\s+/gm, '') // ordered lists
    .replace(/^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/gm, ' ') // thematic breaks
    .replace(/\*\*([^*]+)\*\*/g, '$1') // **bold**
    .replace(/__([^_]+)__/g, '$1') // __bold__
    .replace(/\*([^*]+)\*/g, '$1') // *italic*
    .replace(/~~([^~]+)~~/g, '$1') // ~~strikethrough~~
    .replace(/\|/g, ' ') // table pipes
}

/**
 * Remove citation markers (`[N]`, `[ref:N]`, `[[ref:N]]`) from a scan preview —
 * they read as noise (`[3][4][31]`) in a two-line clamp and can't be clickable
 * chips there. Full-width variants (`【N】`/`［N］`) are normalized to ASCII first
 * so none slip through. The full cited card (Inspector / one-pager) keeps them
 * as real anchors — this only strips them from the flat preview.
 */
export function stripCitationMarkers(text: string): string {
  return normalizeCitationMarkers(text)
    .replace(/\[\[ref:\d+\]\]/g, '')
    .replace(/\[ref:\d+\]/g, '')
    .replace(/\[\d+\]/g, '')
}

/**
 * Turn a finding/signal body — markdown, or a raw `{"title","body"}` JSON
 * envelope — into a flat plain-text preview for the feed scan line: unwrap the
 * envelope, strip markdown, drop citation-marker noise, collapse whitespace.
 */
export function feedPreview(raw: string | null | undefined): string {
  if (!raw) return ''
  const plain = stripCitationMarkers(stripMarkdown(unwrapEnvelope(raw)))
  return plain.replace(/\s+/g, ' ').trim()
}
