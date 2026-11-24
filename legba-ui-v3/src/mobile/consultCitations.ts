/**
 * consultCitations — bind a Consult answer's row references to its cited refs,
 * so the SHARED prose renderer (`CitedProse`) can draw them as citation chips.
 *
 * This is not a second markdown renderer. The answer is rendered by
 * `CitedProse` exactly as a read's prose is (markdown, tables in their own
 * scroller, the chip and its card). What a Consult answer lacks is the ordinal
 * binding a read carries: the final-reply contract
 * (`consult_system_prompt.py`) lists the rows it used on a `cited_refs:` line
 * of bare ids, and the prose names a row by its id in brackets —
 * `[792fd4d7]`, `[792fd4d7-ff16-…]`, `[a1b2c3d4, e5f6a7b8]` or
 * `[[ref:<uuid>]]`. The server's verify pass binds the same tokens the same
 * way (`consult_judging._verify_claims`: a token of 8+ hex characters, matched
 * as a prefix of exactly one cited id).
 *
 * Each such group whose every token resolves to exactly one cited ref is
 * rewritten to `[[ref:k]]` markers (k in order of first appearance) and a
 * `Citation` is built for each k from the ref (its id, kind and the
 * description the server gave it). Everything else is left as written: a
 * group that does not resolve stays literal text, and a bare `[3]` the model
 * wrote with no binding renders as `CitedProse`'s muted "unresolved" chip —
 * never an anchor to a row the answer did not name.
 */
import type { Citation, CitationRefKind } from '@/lib/citationsModel'
import type { ConsultCitedRef } from '@/state/consultSession'

const BRACKET_RE = /\[\[ref:([0-9a-fA-F-]{8,36})\]\]|\[([^[\]\n]{8,400})\](?!\()/g
const REF_TOKEN_RE = /^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{1,12}){0,4}$/
const TOKEN_SPLIT_RE = /[\s,;]+/

export interface BoundAnswer {
  /** The answer with every resolvable row reference rewritten to `[[ref:k]]`. */
  text: string
  /** One citation per `[[ref:k]]` marker, in order. */
  citations: Citation[]
}

function refKindOf(kind: string): CitationRefKind {
  // `finding` and `signal` drill into the record; any other kind the server
  // names (an event, a situation, an observation) is labelled with its own
  // word and resolves in the chip's card rather than by a drill.
  return (kind || 'signal') as CitationRefKind
}

export function consultAnswerCitations(
  answer: string,
  refs: readonly ConsultCitedRef[] | null | undefined,
): BoundAnswer {
  const cited = (refs ?? []).filter((r) => r && typeof r.id === 'string' && r.id.length > 0)
  if (!answer || cited.length === 0) return { text: answer ?? '', citations: [] }
  const lower = cited.map((r) => r.id.toLowerCase())
  const ordinal = new Map<number, number>() // cited index → k
  const citations: Citation[] = []

  const resolve = (token: string): number | null => {
    const t = token.toLowerCase()
    if (!REF_TOKEN_RE.test(t) && !(t.length === 36 && /^[0-9a-f-]+$/.test(t))) return null
    const hits = lower.map((id, i) => (id.startsWith(t) ? i : -1)).filter((i) => i >= 0)
    return hits.length === 1 ? hits[0] : null
  }
  const markerFor = (i: number): string => {
    let k = ordinal.get(i)
    if (k == null) {
      k = ordinal.size + 1
      ordinal.set(i, k)
      const ref = cited[i]
      citations.push({
        marker: `[[ref:${k}]]`,
        refId: ref.id,
        refKind: refKindOf(ref.kind),
        // the deprecated alias, as `citationsModel` writes it: the id on a signal, else empty
        signalId: ref.kind === 'signal' ? ref.id : '',
        ...(ref.description ? { title: ref.description } : {}),
      })
    }
    return `[[ref:${k}]]`
  }

  const text = answer.replace(BRACKET_RE, (whole, single: string | undefined, group: string | undefined) => {
    const tokens = single ? [single] : (group ?? '').split(TOKEN_SPLIT_RE).filter(Boolean)
    if (tokens.length === 0) return whole
    const idx = tokens.map(resolve)
    if (idx.some((i) => i == null)) return whole
    return (idx as number[]).map(markerFor).join('')
  })
  return { text, citations }
}
