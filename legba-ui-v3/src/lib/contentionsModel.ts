/**
 * contentionsModel — the contrary-evidence pass's records, on the reader
 * surface (Program 7a).
 *
 * WHAT A ROW IS, AND THE ONE THING IT IS NOT. Every other retrieval this
 * platform runs points AT a claim: the standing auditor searches for the
 * claim's own query, the fact arbiter compares values already extracted, R2
 * compares claims that already came in. The contrary-evidence pass formulates
 * the COUNTER-query — the strongest opposing proposition — runs it on the free
 * rung, fetches the hits through the auditor's own fences, and records what
 * came back.
 *
 * A `stance` therefore describes a RETRIEVAL, never the claim. `contradicts`
 * means a page this platform fetched and holds states the opposite; it does
 * NOT mean the claim is false, and no surface built on this module may render
 * it as though it did. The route says the same thing in its `note` field, and
 * that field is carried here rather than dropped so a panel can show it.
 *
 * TWO DERIVATIONS, AND THEY DO NOT CARRY THE SAME WEIGHT. `polarity` is R2's
 * closed vocabulary, calibrated against 1,592 live claims when it shipped, and
 * it is the only derivation a COMPOSITION is allowed to render. `negation` is
 * the uncalibrated fallback for a claim that takes no side in that vocabulary
 * — it is served HERE, on the human surface, precisely because a reader can
 * open the counter-ref and judge it, which a composition's prose cannot.
 * The chip says which one it is reading.
 *
 * HONESTY CONTRACT, the same one the rest of the reading kit keeps: absence is
 * absence. No rows, an unreachable route, a read with no id — every one of
 * them yields an EMPTY map and the claim renders exactly as it did before this
 * module existed. Nothing here infers a contention from anything; a chip only
 * ever quotes a row the pass wrote.
 */
import { useQuery } from '@tanstack/react-query'

import { apiGet } from '@/lib/api'

/** The four retrieval outcomes. Not a verdict vocabulary — see the banner. */
export type ContentionStance = 'contradicts' | 'qualifies' | 'none_found' | 'search_failed'

/** Which deterministic rule produced the stance. */
export type ContentionDerivation = 'polarity' | 'negation' | 'none'

/** One page the pass actually FETCHED. A search snippet never becomes a ref. */
export interface ContentionRef {
  url: string
  sha256: string
  chars: number
  /** The page's OWN stated date, discovered from the document. `null` when the
   *  page states none — never today's date, never a guess. */
  published_at: string | null
  extracted: boolean
  fetched_at: string
  status_code: number | null
  stance: string
  quote: string
  /**
   * F1 — what kind of host answered, in the source-class taxonomy's own words
   * (`reporting` / `analysis` / `official` / `state_media`) plus `reference`
   * and `unknown`. That vocabulary is CLOSED and the empty string is not in it,
   * so `''` (or an absent key) is a page the fences never read — a record
   * written before migration 0222 — and reads as {@link NOT_MEASURED}.
   */
  host_class?: string | null
  /**
   * F2 — the date the date gate actually PARSED. Deliberately not the same
   * field as `published_at`, which stays the raw discovery: a stamp the gate
   * could not read is visible as the difference between the two. `null` on a
   * page the fences DID read means the gate found no date it could use, which
   * is a measurement, not an absence of one.
   */
  page_published_at?: string | null
  /** F3 — how many of the claim's subject tokens the matched sentence carried.
   *  A real 0 only exists on a page the fences read; see {@link refFencesRead}. */
  subject_overlap?: number | null
  /** The fence that refused or capped this page — empty on an admissible one. */
  fence?: string | null
}

/** One contention record, as `GET /v3/contentions` serves it. */
export interface ContentionRow {
  claim_id: string
  claim_text: string
  finding_id: string | null
  origin_head_id: string | null
  /** The claim's `[[ref:N]]` ordinal inside `finding_id`. */
  block_ordinal: number | null
  span_role: string | null
  target_id: string | null
  desk_key: string
  analyst_id: string
  query: string
  query_source: string
  query_novel_tokens: number
  stance: ContentionStance
  derivation: ContentionDerivation
  reason: string
  statement: string
  rung: string
  refs: ContentionRef[]
  linked_signals: number
  /**
   * The four fences' numbers for the DECISIVE page (migration 0222). Every one
   * of them is `null` — never a default — when it was not measured, because a
   * row written before 0222 measured none of them and "not measured" is not
   * "measured, and the answer was zero". The route says the same thing in its
   * own docstring; a surface that printed 0 here would invent a reading.
   */
  host_class?: string | null
  page_published_at?: string | null
  subject_overlap?: number | null
  /** F4, and a CLAIM-level count rather than a page one: how many independent
   *  outlets carried a contradicting page. `contradicts` needs
   *  {@link INDEPENDENT_PAGE_BAR}; one admissible page is `qualifies` at most. */
  independent_pages?: number | null
  pipeline_version: string
  retrieved_at: string
  as_of: string
  expires_at: string
  /** Whether the record's shelf life has run out. Rendered, never hidden. */
  live: boolean
}

export interface ContentionsResponse {
  version: string
  /** The route's own contract sentence — a stance is not a verdict. */
  note: string
  scope: string | null
  /** WHICH handle the returned rows matched `scope` on — `target_id`,
   *  `desk_key`, or `both`. The route matches either, so without this a reader
   *  cannot tell "nothing found" from "wrong handle". */
  scope_field?: string | null
  stances: ContentionStance[]
  contentions: ContentionRow[]
}

/** The stances that say something was retrieved and held. */
export const STANCE_BEARING: ReadonlySet<string> = new Set(['contradicts', 'qualifies'])

/** How a fence reading that was never taken is printed. Absence is absence: 0
 *  is a number the fence measured, and these rows can hold neither by default. */
export const NOT_MEASURED = 'not measured'

/** F4's bar. `contradicts` needs TWO pages from two independent outlets, each
 *  past F1–F3; one admissible page is `qualifies` and no more. The number is
 *  shown beside the count so the count means something to a reader who has
 *  never read the pass. */
export const INDEPENDENT_PAGE_BAR = 2

/**
 * `{ordinal: row}` for the records written against ONE published read.
 *
 * Block N IS citation `[[ref:N]]` — `|blocks| == |citations| == |distinct
 * markers|` holds by construction on an assembly row — so the ordinal is the
 * whole join and no hashing happens in the browser.
 *
 * When a claim has several records (the pass re-ran on a later day), the
 * NEWEST wins: the route orders `as_of DESC`, and an older retrieval is
 * history rather than a second opinion. Records that settled nothing
 * (`none_found` / `search_failed`) are skipped — a chip that appeared for
 * "we looked and found nothing" would be a claim the platform has not earned,
 * and it is also the common case by design.
 */
export function contentionsByOrdinal(
  rows: ContentionRow[] | null | undefined,
): Map<number, ContentionRow> {
  const out = new Map<number, ContentionRow>()
  for (const row of rows ?? []) {
    const ordinal = row?.block_ordinal
    if (typeof ordinal !== 'number' || !Number.isFinite(ordinal)) continue
    if (!STANCE_BEARING.has(row.stance)) continue
    if (!out.has(ordinal)) out.set(ordinal, row)
  }
  return out
}

/** The counter-ref a chip and the drill both lead with — the page that carries
 *  the stance, else the first page read. `null` when nothing was fetched. */
export function decisiveRef(row: ContentionRow | null | undefined): ContentionRef | null {
  const refs = row?.refs ?? []
  return refs.find((r) => r.stance === row?.stance) ?? refs[0] ?? null
}

/**
 * The chip's tooltip: ONE sentence, from the row, and it never resolves.
 *
 * "a page ... states the opposite" is a fact about the retrieval. "the claim is
 * false" would be a verdict, and this pass does not render verdicts — so the
 * sentence stops at what was retrieved and names its date.
 */
export function contentionReason(row: ContentionRow): string {
  const ref = decisiveRef(row)
  const verb = row.stance === 'contradicts' ? 'states the opposite' : 'narrows this'
  const when = ref?.published_at ? `, published ${ref.published_at}` : ''
  const host = ref?.url ? hostOf(ref.url) : null
  const rule = row.derivation === 'negation' ? ' (uncalibrated negation rule)' : ''
  const stale = row.live ? '' : ' — expired, not re-checked'
  return (
    `retrieved counter-evidence${rule}: a page this platform holds ${verb}` +
    (host ? ` — ${host}${when}` : '') +
    `${stale}. Not adjudicated.`
  )
}

function hostOf(url: string): string | null {
  try {
    return new URL(url).host
  } catch {
    return null
  }
}

// ---------------------------------------------------------------------------
// The four fences, on the drill (migration 0222)
//
// The chip says a page we hold states the opposite. THE FENCES ARE WHY THAT
// SENTENCE IS ALLOWED TO EXIST: a reference host can never contradict (F1), an
// undated page or one from another year cannot either (F2), the polarity match
// has to land in a sentence carrying the claim's own subject (F3), and one
// admissible page is a qualification — a contradiction takes two independent
// outlets (F4). Until this lane the route served all four and the reader surface
// dropped them, so the drill asserted the conclusion and withheld the test.
//
// Every one of them prints as a reading or as NOT_MEASURED, and never as 0.
// ---------------------------------------------------------------------------

/**
 * Whether the fences were ever run against THIS page.
 *
 * `host_class` is the marker because its producer vocabulary is closed
 * (`_contrary_fences.HOST_CLASSES`) and `host_class_of` returns a member of it
 * for every URL — `unknown` for a host it does not recognise, never `''`. So an
 * empty or absent `host_class` is a page the fence pass never saw, and its
 * `subject_overlap: 0` is a serialisation default rather than an overlap of
 * zero. Nothing else on the ref can tell those two apart.
 */
export function refFencesRead(ref: ContentionRef | null | undefined): boolean {
  return typeof ref?.host_class === 'string' && ref.host_class.length > 0
}

/**
 * F4 as a sentence: the count, the bar, and what falling short of it meant.
 *
 * `independent_pages` counts the CONTRADICTING pages that came from independent
 * outlets, so a `qualifies` record sits below the bar by construction — either
 * because its one admissible page was demoted here, or because no page
 * contradicted at all. Both are said in words; neither is said as a verdict.
 */
export function independentPagesLine(row: ContentionRow): string {
  const n = row.independent_pages
  if (typeof n !== 'number' || !Number.isFinite(n)) return NOT_MEASURED
  // The BAR governs the plural, not the count: "1 of 2 independent pages" is
  // one reading of a two-page requirement, never one page being described.
  const count = `${n} of ${INDEPENDENT_PAGE_BAR} independent pages`
  if (n >= INDEPENDENT_PAGE_BAR) return `${count} — the bar a contradiction needs`
  if (n === 1) return `${count} — one admissible page, not enough to contradict`
  return `${count} — no admissible page contradicted, not enough to contradict`
}

/** The four fences' readings for the DECISIVE page, as the drill's core rows. */
export interface ContentionFences {
  independent_pages: string
  host_class: string
  page_date: string
  subject_overlap: string
}

/**
 * The decisive page's fence readings, each one a reading or `not measured`.
 *
 * The row's own four columns are used rather than the decisive ref's: they ARE
 * the decisive page's numbers (`_contrary_stance` copies them off `best_ref`),
 * and they are the ones the route promises to serve as `null` when unmeasured.
 */
export function contentionFences(row: ContentionRow): ContentionFences {
  const overlap = row.subject_overlap
  return {
    independent_pages: independentPagesLine(row),
    host_class: row.host_class || NOT_MEASURED,
    page_date: row.page_published_at || NOT_MEASURED,
    subject_overlap:
      typeof overlap === 'number' && Number.isFinite(overlap)
        ? `${overlap} of the claim's subject words in the matched sentence`
        : NOT_MEASURED,
  }
}

/** One fetched page as the drill lists it — host class, page date, URL. */
export interface ContentionPage {
  url: string
  host_class: string
  page_date: string
  subject_overlap: string
  /** This PAGE's own stance, which is not the record's: F1 and F3 take a
   *  refused page to `none_found` while the record keeps the best one. */
  stance: string
  /** Present only when a fence fired on this page. */
  fence?: string
}

/**
 * Every page the pass fetched, in the order it read them.
 *
 * The whole list rather than the decisive one: a contradiction that needs two
 * independent outlets cannot be checked against a single page, and a reader who
 * can see that three of the four pages were encyclopedia entries has learned
 * something the count alone does not say.
 */
export function contentionPages(row: ContentionRow | null | undefined): ContentionPage[] {
  return (row?.refs ?? []).map((ref) => {
    const read = refFencesRead(ref)
    const fence = typeof ref.fence === 'string' ? ref.fence : ''
    return {
      url: ref.url,
      host_class: read ? String(ref.host_class) : NOT_MEASURED,
      // A measured page with no `page_published_at` is the date gate reporting
      // that it could read none — the reason an undated page cannot contradict.
      page_date: read
        ? ref.page_published_at || 'no date the gate could read'
        : NOT_MEASURED,
      subject_overlap:
        read && typeof ref.subject_overlap === 'number' && Number.isFinite(ref.subject_overlap)
          ? String(ref.subject_overlap)
          : NOT_MEASURED,
      stance: ref.stance,
      ...(fence ? { fence } : {}),
    }
  })
}

/**
 * The records for one published read, or `null` while unknown.
 *
 * `null` and an empty map are DIFFERENT and the caller must keep them apart:
 * `null` is "not read yet / no read id", an empty map is "read, and nothing was
 * contended". A chip must never appear for the first.
 *
 * An unreachable route resolves to an empty list rather than throwing — a
 * reader surface does not fall over because an optional sidecar is missing, and
 * the pass's own descriptor ships `draft`, so an empty answer is the expected
 * one until it is activated.
 */
export function useReadContentions(findingId: string | null | undefined) {
  const q = useQuery<ContentionRow[]>({
    enabled: Boolean(findingId),
    queryKey: ['read-contentions', findingId],
    staleTime: 60_000,
    queryFn: async () => {
      try {
        const resp = await apiGet<ContentionsResponse>(
          `/v3/contentions?finding_id=${encodeURIComponent(String(findingId))}`,
        )
        return resp?.contentions ?? []
      } catch {
        return []
      }
    },
  })
  return findingId && q.data ? contentionsByOrdinal(q.data) : null
}
