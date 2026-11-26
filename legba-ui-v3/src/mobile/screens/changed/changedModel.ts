/**
 * changedModel — the data behind the phone's landing: ONE read of
 * `GET /v3/since?cursor=<window start>&include=needs_you` per window, and the
 * Changed badge's poll of `GET /v3/since/counts` (its `needs_you`
 * is the same paged-only predicate as the list, `NEEDS_YOU_SQL`).
 *
 *   NEEDS YOU   the alert log's open episodes that PAGED ("needs you"
 *               shows what actually paged). The section also
 *               carries open episodes at medium and above that did not page
 *               (`why: open_at_severity`); the phone leaves those to Watch.
 *   CHANGED     the Board's ranked list (`lib/morningReadBands::changedList`,
 *               as-is) plus the forecasts whose window closed with no
 *               outcome (`forecasts_due`), each a row.
 *   SINCE YOU LAST LOOKED   counts of the same rows after the seen cursor,
 *               computed from the same answer — no second request.
 *
 * The window chips (24 h, 7 d, 30 d) are the one clock (`useWorldState`):
 * a chip asks for its own window once; switching back reads the cache.
 * Pure helpers + one hook.
 */
import { useQuery } from '@tanstack/react-query'
import { apiGet } from '@/lib/api'
import { changedList, type ChangedRow, type ForecastDue, type JudgmentSince } from '@/lib/morningReadBands'
import type { SinceFinding } from '@/lib/wallModel'

/** S-1's "needs you" episode (`registry/alert_log_api.py::NeedsYouEpisode`). */
export interface NeedsYouEpisode {
  id: string
  kind: string
  group?: string | null
  topic?: string | null
  severity: string
  summary: string
  subject_kind: string
  subject_id: string
  opened_at: string
  last_raised_at: string
  last_paged_at?: string | null
  paged_severity?: string | null
  why: 'paged' | 'open_at_severity' | string
  acked_at?: string | null
  snoozed_until?: string | null
}

export interface PhoneSince extends JudgmentSince {
  needs_you?: { items: NeedsYouEpisode[]; total: number; truncated: boolean }
}

/** The window chips: the scrubber's spans the phone offers (no 6 h, no band). */
export const PHONE_WINDOWS = [
  { id: '24h', label: '24 h', ms: 24 * 3_600_000 },
  { id: '7d', label: '7 d', ms: 7 * 24 * 3_600_000 },
  { id: '30d', label: '30 d', ms: 30 * 24 * 3_600_000 },
] as const

export type PhoneWindowId = (typeof PHONE_WINDOWS)[number]['id']

/** The chip a span is (the nearest of the three; the phone never shows another). */
export function phoneWindowOf(spanMs: number): (typeof PHONE_WINDOWS)[number] {
  let best: (typeof PHONE_WINDOWS)[number] = PHONE_WINDOWS[0]
  for (const w of PHONE_WINDOWS) if (Math.abs(w.ms - spanMs) < Math.abs(best.ms - spanMs)) best = w
  return best
}

/** The read's path: the window's start, to the minute, so a refetch inside a minute asks the same thing. */
export function changedPath(windowMs: number, now = Date.now()): string {
  const end = Math.floor(now / 60_000) * 60_000
  const cursor = new Date(end - windowMs).toISOString()
  return `/v3/since?cursor=${encodeURIComponent(cursor)}&include=needs_you`
}

export const changedKey = (windowId: string) => ['phone', 'changed', windowId] as const

export function useChanged(windowId: PhoneWindowId) {
  const w = PHONE_WINDOWS.find((x) => x.id === windowId) ?? PHONE_WINDOWS[0]
  return useQuery<PhoneSince>({
    queryKey: changedKey(w.id),
    queryFn: () => apiGet<PhoneSince>(changedPath(w.ms)),
    // the list never moves under the thumb: a person refreshes (the top bar)
    staleTime: 5 * 60_000,
    refetchOnWindowFocus: false,
  })
}

/** The counts route's answer (`registry/since_api.py::SinceCountsResponse`). */
export interface PhoneSinceCounts {
  cursor: string
  server_now: string
  counts: Record<string, number>
  /** Open episodes that PAGED since the cursor — the list's own predicate. */
  needs_you: number
  changed: number
  forecasts_due: number
}

/** The badge poll's path: the same window start as the list, to the minute. */
export function countsPath(windowMs: number, now = Date.now()): string {
  const end = Math.floor(now / 60_000) * 60_000
  return `/v3/since/counts?cursor=${encodeURIComponent(new Date(end - windowMs).toISOString())}`
}

/** How often the badge asks (the server reuses an answer 30 s per caller). */
export const BADGE_POLL_MS = 60_000

/** The Changed badge's count: what paged in the window, from the counts
 * route (cheap; polled), so the badge stays current on every tab without
 * re-reading the list. */
export function useNeedsYouCount(windowId: PhoneWindowId) {
  const w = PHONE_WINDOWS.find((x) => x.id === windowId) ?? PHONE_WINDOWS[0]
  return useQuery<PhoneSinceCounts>({
    queryKey: ['phone', 'counts', w.id],
    queryFn: () => apiGet<PhoneSinceCounts>(countsPath(w.ms)),
    staleTime: BADGE_POLL_MS,
    refetchInterval: BADGE_POLL_MS,
    refetchOnWindowFocus: true,
  })
}

/** The badge figure: the counts route's paged-only `needs_you`; until it has
 * answered (or on a server without it), the paged rows of the list already held. */
export function badgeCount(counts: PhoneSinceCounts | null | undefined, since: PhoneSince | null | undefined): number {
  if (counts && typeof counts.needs_you === 'number') return counts.needs_you
  return pagedOnly(since).length
}

/** Only what paged, worst first as the server ranks them. */
export function pagedOnly(since: PhoneSince | null | undefined): NeedsYouEpisode[] {
  return (since?.needs_you?.items ?? []).filter((e) => e.why === 'paged' || !!e.last_paged_at)
}

/** One row of the phone's Changed list. */
export interface PhoneChangedRow {
  key: string
  kind: 'finding' | 'situation' | 'band' | 'alert' | 'forecast_due'
  /** What a tap opens: a read, the Record, or nothing. */
  open: { read: string } | { record: { kind: string; id: string; label?: string } } | { subject: string } | null
  headline: string
  /** The severity or trust word, or null when the row carries none. */
  word: string | null
  subject: string
  /** The subject's id, when the row has one (a subject page lists its own). */
  targetId: string | null
  at: string
  chip: string
}

const CHIP: Record<PhoneChangedRow['kind'], string> = {
  finding: 'changed',
  situation: 'situation',
  band: 'changed',
  alert: 'alert',
  forecast_due: 'forecast due',
}

const DUE_WORDS: Record<string, string> = {
  in_grace: 'in grace',
  awaiting: 'awaiting confirmation',
  expired: 'expired unresolved',
}

export function changedRows(since: PhoneSince | null | undefined, nameOf: (id: string) => string): PhoneChangedRow[] {
  if (!since) return []
  const rows: PhoneChangedRow[] = changedList(since).map((r: ChangedRow) => ({
    key: `${r.kind}:${r.id}`,
    kind: r.kind,
    open:
      r.kind === 'finding'
        ? { read: r.id }
        : r.kind === 'situation' || r.kind === 'alert'
          ? { record: { kind: r.rowKind, id: r.id, label: r.headline } }
          : r.targetId
            ? { subject: r.targetId }
            : null,
    // a band's dimension is a machine id (`leadership_transition`): said in words
    headline: r.kind === 'band' ? r.headline.replace(/^([a-z0-9]+(?:_[a-z0-9]+)+):/, (_m, d: string) => `${d.charAt(0).toUpperCase()}${d.slice(1).replace(/_/g, ' ')}:`) : r.headline,
    word: r.severity,
    subject: r.targetId ? nameOf(r.targetId) : r.deskLabel,
    targetId: r.targetId,
    at: r.at,
    chip: CHIP[r.kind],
  }))
  for (const f of (since.forecasts_due?.items ?? []) as ForecastDue[]) {
    rows.push({
      key: `forecast_due:${f.id}`,
      kind: 'forecast_due',
      open: { subject: f.region },
      headline: f.resolution_test,
      word: DUE_WORDS[f.mark] ?? f.mark,
      subject: nameOf(f.region),
      targetId: f.region,
      at: f.window_end,
      chip: CHIP.forecast_due,
    })
  }
  return rows
}

/** How many rows landed after the seen cursor (null: no cursor yet, everything is new). */
export function sinceCounts(
  since: PhoneSince | null | undefined,
  rows: readonly PhoneChangedRow[],
  seenAt: string | null,
): { changed: number; needsYou: number } {
  const t = seenAt ? Date.parse(seenAt) : NaN
  const after = (iso: string | null | undefined) => !Number.isFinite(t) || (!!iso && Date.parse(iso) > t)
  return {
    changed: rows.filter((r) => r.kind !== 'forecast_due' && after(r.at)).length,
    needsYou: pagedOnly(since).filter((e) => after(e.last_paged_at ?? e.opened_at)).length,
  }
}

/** The new finding a read page opens, from the answer it was listed in. */
export function sinceFinding(since: PhoneSince | null | undefined, id: string): SinceFinding | null {
  return (since?.new_findings?.items ?? []).find((f) => f.id === id) ?? null
}
