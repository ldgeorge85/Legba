/**
 * CitedRecords — the records a Consult answer cites, READABLE (U-6; the owner
 * saw about thirty lines of "signal: <uuid>").
 *
 *   Records cited · 31   ▸ show
 *     Port strike enters its third day            reuters.com · 12 Nov
 *     Ministry denies the tariff report           apnews.com · 11 Nov
 *     … and 4 records this answer does not describe
 *
 * Collapsed to the count by default; expanded, each record is its title, its
 * outlet (or source) and its date. The id is only in the hover title and in
 * the Record sheet a click opens — never printed.
 *
 * WHERE THE WORDS COME FROM — only what the client already holds, no new
 * fetch: (1) the answer's own tool results, which carry the rows the model
 * read (`id`, `title`, `title_en`, `source_url`, `produced_at`, …); (2) the
 * cited ref's own `description`; (3) rows already in this page's query cache
 * (a feed the reader has open). A record none of these describe is counted
 * as such, honestly. A batched by-id lookup would describe the rest; there is
 * no such route today (noted for the fetch-diet lane).
 */
import { useContext, useMemo, useState } from 'react'
import { QueryClientContext } from '@tanstack/react-query'
import { RecordLink } from '@/components/inspector/RecordLink'
import { selectionKindOf } from '@/state/selection'
import { selectionKindLabel } from '@/lib/subjectNoun'
import type { ConsultCitedRef, ConsultToolCall } from '@/state/consultSession'

export interface RecordWords {
  title: string | null
  outlet: string | null
  date: string | null
}

const TITLE_KEYS = ['title_en', 'title', 'headline', 'name', 'label', 'summary'] as const
const OUTLET_KEYS = ['outlet', 'source_name', 'publisher', 'source_title'] as const
const URL_KEYS = ['source_url', 'canonical_url', 'url', 'link'] as const
const DATE_KEYS = ['published_at', 'produced_at', 'fetched_at', 'occurred_at', 'created_at', 'date'] as const

/**
 * The kind the server gives a cited id that no substrate table holds
 * (`registry/cited_ref_kinds.py::UNRESOLVED`): shown, never linked — a walk
 * on it can only 404.
 */
export const UNRESOLVED = 'unresolved'
const UNRESOLVED_TITLE = 'This record is not in the store, so it cannot be opened.'

function str(v: unknown): string | null {
  return typeof v === 'string' && v.trim() ? v.trim() : null
}

function hostOf(url: string | null): string | null {
  if (!url) return null
  try {
    return new URL(url).hostname.replace(/^www\./, '')
  } catch {
    return null
  }
}

function wordsOf(o: Record<string, unknown>): RecordWords {
  let title: string | null = null
  for (const k of TITLE_KEYS) {
    title = str(o[k])
    if (title) break
  }
  let outlet: string | null = null
  for (const k of OUTLET_KEYS) {
    outlet = str(o[k])
    if (outlet) break
  }
  if (!outlet) for (const k of URL_KEYS) outlet ??= hostOf(str(o[k]))
  if (!outlet) outlet = str(o.source_id) ?? (typeof o.source === 'string' ? str(o.source) : null)
  let date: string | null = null
  for (const k of DATE_KEYS) {
    date = str(o[k])
    if (date) break
  }
  return { title, outlet, date }
}

/**
 * Index every object with a string `id` (and something to say about it) in
 * `roots`, breadth-first and bounded — a tool result can be large, and this
 * runs in the render path.
 */
export function indexRecordWords(roots: readonly unknown[], want: ReadonlySet<string>, budget = 20_000): Map<string, RecordWords> {
  const out = new Map<string, RecordWords>()
  const queue: unknown[] = [...roots]
  let head = 0
  while (head < queue.length && head < budget && out.size < want.size) {
    const v = queue[head++]
    if (!v || typeof v !== 'object') continue
    if (Array.isArray(v)) {
      for (const x of v) queue.push(x)
      continue
    }
    const o = v as Record<string, unknown>
    const id = str(o.id) ?? str(o.signal_id) ?? str(o.finding_id)
    if (id && want.has(id) && !out.has(id)) {
      const w = wordsOf(o)
      if (w.title || w.outlet) out.set(id, w)
    }
    for (const x of Object.values(o)) if (x && typeof x === 'object') queue.push(x)
  }
  return out
}

function shortDate(iso: string | null): string | null {
  if (!iso) return null
  const t = Date.parse(iso)
  if (!Number.isFinite(t)) return null
  return new Date(t).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: undefined })
}

function cacheRoots(qc: { getQueryCache: () => { getAll: () => { state: { data: unknown } }[] } } | undefined): unknown[] {
  if (!qc) return []
  try {
    return qc
      .getQueryCache()
      .getAll()
      .map((q) => q.state.data)
      .filter((d) => d != null)
  } catch {
    return []
  }
}

export function CitedRecords({ refs, toolCalls }: { refs: readonly ConsultCitedRef[]; toolCalls?: readonly ConsultToolCall[] }) {
  const [open, setOpen] = useState(false)
  const qc = useContext(QueryClientContext)
  const words = useMemo(() => {
    const want = new Set(refs.map((r) => r.id))
    const fromTools = indexRecordWords((toolCalls ?? []).map((t) => t.result), want)
    // Only the records the tools did not describe are looked for in the cache.
    const rest = new Set([...want].filter((id) => !fromTools.has(id)))
    const fromCache = open && rest.size > 0 ? indexRecordWords(cacheRoots(qc as never), rest) : new Map<string, RecordWords>()
    return new Map([...fromCache, ...fromTools])
  }, [refs, toolCalls, qc, open])

  const rows = refs.map((r) => {
    const w = words.get(r.id)
    const title = w?.title ?? str(r.description)
    return { ref: r, title, outlet: w?.outlet ?? null, date: shortDate(w?.date ?? null) }
  })
  const described = rows.filter((r) => r.title)
  const bare = rows.filter((r) => !r.title)
  return (
    <div data-testid="consult-cited" data-count={refs.length}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex items-center gap-2 text-label text-ink-3 hover:text-ink-1"
        data-testid="consult-cited-toggle"
      >
        <span className="uppercase tracking-[0.06em]">Records cited</span>
        <span className="font-mono">{refs.length}</span>
        <span aria-hidden>{open ? '▾ hide' : '▸ show'}</span>
      </button>
      {open && (
        <ul className="mt-1 space-y-1" data-testid="consult-cited-list">
          {described.map(({ ref, title, outlet, date }, j) => (
            <li key={`${ref.kind}:${ref.id}`} className="flex min-w-0 items-baseline gap-2 text-body" data-testid={`consult-cited-row-${j}`}>
              <span className="min-w-0 flex-1 truncate">
                {ref.kind === UNRESOLVED ? (
                  <span className="text-ink-2" title={UNRESOLVED_TITLE} data-testid="consult-cited-unresolved">
                    {title}
                  </span>
                ) : (
                  <RecordLink
                    kind={selectionKindOf(ref.kind)}
                    rowKind={ref.kind}
                    id={ref.id}
                    label={title!}
                    origin="consult"
                    mono={false}
                    title={`${selectionKindLabel(selectionKindOf(ref.kind))} · ${ref.id} — open the record`}
                    className="text-ink-1 decoration-ink-3 hover:text-point"
                  />
                )}
              </span>
              {(outlet || date) && (
                <span className="shrink-0 font-mono text-label text-ink-3" data-testid="consult-cited-meta">
                  {[outlet, date].filter(Boolean).join(' · ')}
                </span>
              )}
            </li>
          ))}
          {bare.length > 0 && (
            <li className="text-label text-ink-3" data-testid="consult-cited-bare">
              {described.length > 0 ? 'and ' : ''}
              {bare.length} {bare.length === 1 ? 'record' : 'records'} this answer does not describe:{' '}
              {bare.map(({ ref }, j) => (
                <span key={`${ref.kind}:${ref.id}`}>
                  {j > 0 && ', '}
                  {ref.kind === UNRESOLVED ? (
                    <span title={UNRESOLVED_TITLE} data-testid="consult-cited-unresolved">
                      record {j + 1}
                    </span>
                  ) : (
                    <RecordLink
                      kind={selectionKindOf(ref.kind)}
                      rowKind={ref.kind}
                      id={ref.id}
                      label={`${selectionKindLabel(selectionKindOf(ref.kind)).toLowerCase()} ${j + 1}`}
                      origin="consult"
                      mono={false}
                      title={`${ref.kind} · ${ref.id} — open the record`}
                    />
                  )}
                </span>
              ))}
            </li>
          )}
        </ul>
      )}
    </div>
  )
}
