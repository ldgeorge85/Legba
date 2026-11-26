/**
 * budgets — the walk's pass/fail rules, kept apart from the browser so they
 * are unit-tested (walk.test.ts) and read in one place.
 *
 * A budget is ENFORCED (a breach fails the walk) or REPORTED (the number is
 * printed for the record and never fails). The reader text floor is enforced
 * since U-3 landed the type scale on the reader surfaces; `--report-text-floor`
 * turns it back into a reported number.
 */
import { READER_ROLES, readerEngineHits, readerSurfacesMeasured } from './engineWords.mjs'

/*
 * The landing's request budgets (U-5, 2026-11-13; tightened by U-4,
 * 2026-11-14). Fixture: U-5's after-walk measured 22 requests, 0.69 MB,
 * largest 0.25 MB (before U-5: 39 requests, 1.52 MB, 6 duplicates). U-4's
 * after-walk measured 21 requests and 0.71 MB; its largest response is the
 * time band's `/v3/events?limit=500`, 0.37 MB, because U-4 grew the fixture's
 * events from 10 rows to 500 placed ones (the globe's stress set), so the
 * fixture's largest-response budget is 0.45 MB.
 *
 * Live: before U-4 the landing was ~29 requests and ~79 MB, of which ~72 MB
 * was the World map's own reads (`/findings?limit=500` ~29 MB,
 * `country_composition&limit=200` ~23 MB, the `/signals` pager ~18 MB). With
 * the globe the map reads, at rest, the subject reads at verdict weight
 * (`/v3/map/subject_reads`, ~0.06 MB live for 200 rows, priced by SELECT on
 * 2026-11-14), one series (~0.04 MB) and the forecast ledger (~0.03 MB); the
 * roster and the since-diff it shares with the shell. So the live landing is
 * priced at ~26 requests and ~7–8 MB. These live figures are PRICED, not
 * walked (the lane had no live token): the first live walk after the deploy
 * should confirm them and may tighten them further.
 */
/*
 * U-6 (the frame): the Board's landing now SHOWS the at-a-glance panel (the
 * Wall) beside the map — under U-2 it was a hidden tab behind the read, which
 * U-5's query regions kept from fetching. Showing it costs its own reads (the
 * situations by state, the source roster, the analyst cadence, the
 * geo-convergence channel): 29 requests and 0.85 MB on fixtures (measured,
 * the rail's prior-conversation list deferred to a click). The ceilings move
 * by exactly that: requests 26 → 29 (live 34 → 37); bytes 0.85 → 0.95 MB on
 * fixtures for headroom over the measured 0.85 MB. Live bytes and the largest
 * response are unchanged (the Wall's reads are small; the map's dominate).
 */
/*
 * U-7 (the globe in the frame, 2026-11-14) — the two lanes reconciled.
 * Fixture, measured on the merged landing: 29 requests, 0.99 MB, largest
 * 0.37 MB (the time band's `/v3/events?limit=500`, the globe's stress set).
 * Ceilings with modest headroom: 31 requests, 1.10 MB, 0.45 MB.
 * Live, kept at the globe lane's tightened 12 MB and 6 MB and the frame's 37
 * requests. Expected AFTER the registry is rebuilt with the three map routes:
 * about 32–34 requests (the globe's priced ~26 plus the At a glance panel's
 * own reads, which the frame measured at +3 on fixtures and +3 live) and
 * about 8 MB. BEFORE the rebuild the map's subject reads answer 404 and fall
 * back to `/findings?analyst_id=country_composition&limit=200` (~23 MB live),
 * so a live walk then FAILS the byte (~31 MB) and largest-response (~23 MB)
 * budgets by design, and its series and incidents-at-sea rows say they cannot
 * be read; the fixture walk's `--no-map-routes` reproduces that fallback.
 */
const LANDING_REQUESTS_FIXTURE = 31
const LANDING_REQUESTS_LIVE = 37
const LANDING_BYTES_FIXTURE = 1_100_000
const LANDING_BYTES_LIVE = 12_000_000
const LARGEST_FIXTURE = 450_000
const LARGEST_LIVE = 6_000_000

/*
 * U-9 (the Watch surface, 2026-11-14). Its own landing: the shell's reads
 * (identity, the subject roster, the health line, the rail) plus the page's
 * three — the alert log, the drift summary and, only when flags are open,
 * their list; the source roster is the health line's read, shared. Fixture,
 * measured on the U-9 branch: 17 requests, 0.50 MB (largest the time band's
 * `/v3/events?limit=500`, 0.37 MB), 0 duplicates; ceilings 20 and 0.60 MB.
 */
const WATCH_REQUESTS_FIXTURE = 20
const WATCH_REQUESTS_LIVE = 24
const WATCH_BYTES_FIXTURE = 600_000
const WATCH_BYTES_LIVE = 2_000_000

/*
 * The phone landing (M-0a, 2026-11-14). Before: the navigator read
 * `/findings?analyst_id_in=…&limit=80` at full weight — 0.23 MB on fixtures
 * (10 recorded rows) and 9.2 MB live (80 rows with every block, span and
 * cited signal). After: the `fields=summary` projection, and a read fetched
 * at full weight only when it is opened. Fixture, measured after: 5 API
 * requests, 0.024 MB, the largest the subject roster (0.017 MB) — the
 * navigator's own read is 0.006 MB; opening the World read: 1 request,
 * 0.034 MB. Before (the audit's walk of the same fixtures): 5 requests,
 * 0.23 MB, the largest the full-weight list at 0.21 MB.
 * Live is PRICED, not walked: 80 summary rows at ~0.6 kB a row (the fixture's
 * summary rows measure 0.55–0.75 kB) ≈ 0.05–0.06 MB, plus the journal summary,
 * the auth probe, the tiers and the subject roster (the roster is unpriced: its
 * fixture is cut to 10 rows) — so 0.4 MB (largest 0.25 MB)
 * live leaves room, and any return of a full-weight list (MBs) fails it.
 */
/* Phone shell: the landing is Changed — the identity,
   ONE `/v3/since?include=needs_you` for the window, the seen cursor, the
   subject roster. Budget: ≤ 10 requests and ≤ 0.2 MB on fixtures, ≤ 12 and
   ≤ 0.8 MB live.
   The largest response is the since envelope (fixture ~0.03 MB; live, priced:
   50 rows a section at ~0.3 kB plus 50 needs-you episodes ≈ 0.1–0.2 MB) or the
   roster. The live largest is 0.25 MB: the target roster
   (descriptors?family=target) is 0.279 MB at full weight but is read at
   fields=summary (73 KB). */
const PHONE_REQUESTS_FIXTURE = 10
const PHONE_REQUESTS_LIVE = 12
const PHONE_BYTES_FIXTURE = 200_000
const PHONE_BYTES_LIVE = 800_000
const PHONE_LARGEST_FIXTURE = 80_000
const PHONE_LARGEST_LIVE = 250_000
/* One full read on open: a world read is ~0.45 MB live (U-5's measure), a
   country read 20–40 kB; an Assessment brings its underlying world read. */
const PHONE_OPEN_BYTES_FIXTURE = 80_000
const PHONE_OPEN_BYTES_LIVE = 1_200_000

export const DEFAULT_BUDGETS = {
  /** No single main-thread task may run longer than this (ms). */
  longTaskMs: 1000,
  /** Landing ready (shell drawn and the API quiet for 1 s), from navigation (ms). */
  landingReadyMs: { fixture: 8000, live: 20000 },
  /** Viewports checked for horizontal page overflow. */
  overflowWidths: [1680, 1366, 1920, 2560, 3840, 390],
  /** Reader-surface text floor (px): the four reader surfaces, the Record sheet, the phone. */
  textFloorPx: 12,
  enforceTextFloor: true,
  /** The Consult question box must be at least this wide at 1366 and 1680 (px). */
  consultMinWidthPx: 300,
  /** Consult's answers take at least this share of the dock's height (when the dock is open). */
  consultTranscriptMinFraction: 0.6,
  /** A count shown before any data was read ("0 …" while every API answer is held). */
  zeroBeforeRead: 0,
  /** Page errors and console errors (WebSocket and refused-write noise excluded). */
  consoleErrors: 0,
  /** Machine ids as titles on the phone reader. */
  phoneMachineIds: 0,
  /** Engine words on a reader surface walked as a reader (`engineWords.mjs`). */
  readerEngineWords: 0,
  /**
   * THE LANDING'S REQUEST BUDGET (U-5): what the landing at 1680 asks the
   * registry for, from navigation until 3 s past "ready". Set from U-5's
   * after-numbers with modest headroom; the live figures are the fixture
   * pattern priced at live response sizes.
   */
  landingRequests: { fixture: LANDING_REQUESTS_FIXTURE, live: LANDING_REQUESTS_LIVE },
  landingBytes: { fixture: LANDING_BYTES_FIXTURE, live: LANDING_BYTES_LIVE },
  largestResponseBytes: { fixture: LARGEST_FIXTURE, live: LARGEST_LIVE },
  /** Identical requests (same path and query) asked twice in the landing window. */
  duplicateRequests: 0,
  /** Query fetches made for a region nobody can see (a hidden tab, the closed palette). */
  hiddenRegionFetches: 0,
  // ── THE FRAME — budgets that catch layout failures in the shell
  // (a 380 px Consult ribbon at 1920; at 3840 tiny text, a dead band between
  // the map and the read, Consult a sliver).
  /**
   * No full-height strip of the main area with nothing in it (no text, no
   * map, no control) wider than this fraction of the viewport.
   */
  emptyBandMaxFraction: 0.06,
  /**
   * Body text: the median size of the visible text in the main area (by
   * characters), per viewport width (px). The console's text is mostly its
   * 12 px label and meta size, so the floor is that size times the frame's
   * root step (16 px below 2200, 18 from 2200, 24 from 3000): 12 · 13.5 · 18.
   * About 3.4 mm on a 24-inch 1920 screen and 3.2 mm on a 32-inch 3840 one;
   * the U-2 shell measured 12–13 px at 3840 (about 2.2 mm) and fails it.
   */
  bodyTextMinPx: { 1366: 12, 1680: 12, 1920: 12, 2560: 13.5, 3840: 18 },
  /** The chat's reading width (the conversation column), in rem of the root size. */
  chatWidthRem: [30, 52],
  /** The splitters the walk drags must move AND be remembered across a reload. */
  draggables: ['the chat divider', 'the centre edge', 'a panel splitter'],
  /** The Board map ready (drawn with its data), from navigation (ms). */
  mapReadyMs: { fixture: 12000, live: 20000 },
  /** Marks the map may draw at rest (the layer caps summed: 250 + 250 + 250 + 60 + 2000 + 500). */
  mapMarksCap: 3310,
  /**
   * THE WATCH PAGE (U-9), opened by its address on a fresh page: what it asks
   * the registry for, from navigation until 3 s past the alert log drawn.
   * Fixture, measured on this branch: see the U-9 note below; live is PRICED
   * (the alert log ~0.13 MB for a week, source firing ~0.04 MB, the drift
   * summary under 1 kB; the flag list only when flags are open).
   */
  watchRequests: { fixture: WATCH_REQUESTS_FIXTURE, live: WATCH_REQUESTS_LIVE },
  watchBytes: { fixture: WATCH_BYTES_FIXTURE, live: WATCH_BYTES_LIVE },
  // ── THE RECORD SHEET (U-10) — opened from a Board row and from a Consult
  // citation. Its own reads are the lineage walk at depth 1, one receipts
  // call (at most 8 ids) and one integrity call; the stored text is a fourth,
  // only on "Show what was received", capped at 20,000 characters. None of
  // them may be in the landing.
  /** From the click to the chain read to its last step (ms). */
  recordSheetReadyMs: { fixture: 5000, live: 15000 },
  /** The sheet's own reads per open (lineage, receipts, integrity). */
  recordSheetRequests: 3,
  /** Their bytes per open. Live: a finding's lineage body carries its citations' passages. */
  recordSheetBytes: { fixture: 150_000, live: 2_000_000 },
  /** Reads made by "Show what was received". */
  recordTextRequests: 1,
  /**
   * THE LEDGER (U-11): what opening the Ledger page asks for, from the click
   * until the API is quiet. Its own reads are four light ones — the forecasts,
   * the scorecard, the daily anchors (60 rows) and the evaluation record — the
   * forecasts and the scorecard usually shared with the map's forecast layer,
   * plus the Accuracy tab's routes only if it is brought forward. Fixture
   * measured at U-11 (first open, cold): 3 requests, 0.01 MB; ceilings with headroom. Live
   * PRICED by GET on 2026-11-14, not walked: forecasts ~0.03 MB,
   * scorecard ~0.01 MB, anchors ~0.04 MB at 60 rows; the evaluation record
   * (served since E-3) a few KB per published run.
   */
  ledgerRequests: { fixture: 8, live: 10 },
  ledgerBytes: { fixture: 250_000, live: 1_000_000 },
  // ── THE PHONE (M-0a, 2026-11-14) — the checks that would have caught the
  // phone audit: a 9.2 MB landing read, raw markdown and untappable citations
  // in a Consult answer, a question box squeezed by two extra buttons, and an
  // icon that came back as index.html.
  /** The phone landing (`/` redirected to `/m/`, navigation to quiet): API requests, bytes, the largest response. */
  phoneLandingRequests: { fixture: PHONE_REQUESTS_FIXTURE, live: PHONE_REQUESTS_LIVE },
  phoneLandingBytes: { fixture: PHONE_BYTES_FIXTURE, live: PHONE_BYTES_LIVE },
  phoneLargestResponseBytes: { fixture: PHONE_LARGEST_FIXTURE, live: PHONE_LARGEST_LIVE },
  /** Opening one read from the phone list: requests and bytes (one full row, plus an Assessment's underlying read). */
  phoneOpenRequests: 2,
  phoneOpenBytes: { fixture: PHONE_OPEN_BYTES_FIXTURE, live: PHONE_OPEN_BYTES_LIVE },
  /** The question box's share of the phone composer row, at 360 and 390. */
  phoneComposerMinFraction: 0.6,
  /** Every citation in a phone Consult answer is a button reachable ±22 px from its centre (a 44 px target). */
  phoneCitationTargetPx: 44,
  // ── THE PHONE SHELL ──
  /** API requests and bytes per phone screen, its own reads from the tap to quiet (fixture; live is priced separately). */
  phoneScreens: {
    'Changed (landing)': { requests: 10, bytes: 200_000 },
    Read: { requests: 2, bytes: 300_000 },
    'Record peek': { requests: 1, bytes: 150_000 },
    'Record page': { requests: 3, bytes: 150_000 },
    'Map (drawn)': { requests: 4, bytes: 150_000 },
    'Watch · Alerts': { requests: 8, bytes: 300_000 },
    'Watch · silences': { requests: 8, bytes: 300_000 },
    'Watch · edits': { requests: 8, bytes: 300_000 },
    'Alert page': { requests: 1, bytes: 150_000 },
    Ledger: { requests: 4, bytes: 100_000 },
    Evaluations: { requests: 1, bytes: 100_000 },
    Subjects: { requests: 6, bytes: 300_000 },
    'Subject page': { requests: 6, bytes: 300_000 },
    Watch: { requests: 8, bytes: 300_000 },
    Settings: { requests: 1, bytes: 50_000 },
    Consult: { requests: 0, bytes: 0 },
    // M-3g: the answer itself is mocked in the page (never reaches the API);
    // what is left is the shell's own reads around it
    'Consult (answer)': { requests: 2, bytes: 50_000 },
    'Consult → Record peek': { requests: 1, bytes: 150_000 },
    'Map (opened by Consult)': { requests: 4, bytes: 150_000 },
    'Consult (after Undo)': { requests: 2, bytes: 50_000 },
  },
  /** The longest task on any phone screen (ms, unthrottled). */
  phoneLongTaskMs: 200,
  /** The phone's JS, gzip: the initial entry, and everything the landing loads before any tap. */
  phoneInitialJsGz: 260_000,
  phoneLandingJsGz: 365_000,
}

/** The Record sheet's own routes, and the bounds each must carry (U-10). */
const RECORD_ROUTES = [
  { name: 'lineage', re: /\/api\/v1\/lineage\/[a-z_]+\/[^/?]+\?(?=.*direction=upstream)(?=.*depth=1(?:&|$))/ },
  { name: 'receipts', re: /\/api\/v1\/v3\/record\/receipts\?/ },
  { name: 'integrity', re: /\/api\/v1\/v3\/record\/integrity\?/ },
  { name: 'text', re: /\/api\/v1\/v3\/record\/version\/\d+\/text(?:\?|$)/ },
]

/** Which Record-sheet route a request is, or null. */
export function recordRouteOf(url) {
  const hit = RECORD_ROUTES.find((r) => r.re.test(url))
  return hit ? hit.name : null
}

/** A sheet read that breaks its bound (too many ids, an uncapped or oversized text read). */
export function recordReadUnbounded(url) {
  const route = recordRouteOf(url)
  const q = new URLSearchParams(url.split('?')[1] ?? '')
  if (route === 'receipts') return (q.get('ids') ?? '').split(',').filter(Boolean).length > 8
  if (route === 'text') {
    const n = Number(q.get('max_chars'))
    return !Number.isFinite(n) || n < 1 || n > 20000
  }
  return false
}

/**
 * The Record sheet's reads in one phase of the walk's request log: its own
 * routes (counted against the budget), their bytes, any that break a bound,
 * and the other reads that happened while it opened (reported only).
 */
export function recordSheetStats(log, phase) {
  const rows = (log ?? []).filter((r) => r.phase === phase)
  const own = rows.filter((r) => recordRouteOf(r.url))
  return {
    requests: own.length,
    bytes: own.reduce((n, r) => n + (r.bytes ?? 0), 0),
    routes: own.map((r) => recordRouteOf(r.url)),
    unbounded: own.filter((r) => recordReadUnbounded(r.url)).map((r) => r.url),
    others: rows.filter((r) => !recordRouteOf(r.url)).map((r) => r.url.split('?')[0]),
  }
}

/** The body-text floor at a width: the step at or below it. */
export function bodyTextFloorAt(width, table = DEFAULT_BUDGETS.bodyTextMinPx) {
  let best = null
  for (const [w, px] of Object.entries(table)) if (Number(w) <= width && (best == null || Number(w) > best[0])) best = [Number(w), px]
  return best ? best[1] : 12
}

/**
 * The landing's request pattern, from the walk's request log (U-5).
 *
 *   requests     API requests issued in the landing window
 *   bytes        response bytes in that window
 *   findingsBytes  of which `/findings` (the route the 2026-11-13 live walk
 *                found carrying 164 MB of 196 MB)
 *   largest      the largest single response
 *   duplicates   requests whose exact path + query was already asked in the
 *                window (one shared query layer makes this 0)
 *   hidden       fetches the console's own query log attributes to a tile
 *                that was not visible (`window.__legbaFetchLog`, written by
 *                `lib/queryLayer.ts`); null when the bundle keeps no log
 */
export function landingStats(log, phase = 'landing', fetchLog = null) {
  const rows = (log ?? []).filter((r) => r.phase === phase)
  const seen = new Map()
  for (const r of rows) seen.set(r.url, (seen.get(r.url) ?? 0) + 1)
  const duplicates = [...seen].filter(([, n]) => n > 1).map(([url, n]) => ({ url, n }))
  const largest = rows.reduce((m, r) => ((r.bytes ?? 0) > (m?.bytes ?? -1) ? r : m), null)
  const routeOf = (u) => u.split('?')[0]
  const hidden = Array.isArray(fetchLog) ? fetchLog.filter((e) => e && e.hidden) : null
  return {
    requests: rows.length,
    bytes: rows.reduce((n, r) => n + (r.bytes ?? 0), 0),
    findingsRequests: rows.filter((r) => routeOf(r.url).endsWith('/findings')).length,
    findingsBytes: rows.filter((r) => routeOf(r.url).endsWith('/findings')).reduce((n, r) => n + (r.bytes ?? 0), 0),
    largest: largest ? { url: largest.url, bytes: largest.bytes } : null,
    duplicates,
    duplicateRequests: duplicates.reduce((n, d) => n + d.n - 1, 0),
    hidden: hidden == null ? null : hidden.map((e) => `${e.tile ?? '?'}: ${e.key}`),
  }
}

/**
 * The request that ended last in a phase — what held the landing's "ready"
 * (the API quiet for a second) open. Reported beside the figure (e.g. a slow
 * `/v3/system/judge-stats` at 18–22 s; the console draws the footer's judge
 * figure as "not read yet" until it answers).
 */
export function heldBy(log, phase = 'landing') {
  let last = null
  for (const r of log ?? []) {
    if (r.phase !== phase || r.ms == null) continue
    const end = r.start + r.ms
    if (!last || end > last.end) last = { url: r.url, ms: r.ms, end }
  }
  return last ? { url: last.url, ms: last.ms } : null
}

/**
 * Evaluate a finished walk result against the budgets.
 * Returns { failures: string[], table: Array<{check, budget, value, verdict}> }.
 */
export function evaluate(result, budgets = DEFAULT_BUDGETS, mode = 'fixture') {
  const table = []
  const failures = []
  const row = (check, budget, value, ok, enforced = true) => {
    const verdict = ok ? 'pass' : enforced ? 'FAIL' : 'report'
    table.push({ check, budget, value, verdict })
    if (!ok && enforced) failures.push(`${check}: ${value} (budget ${budget})`)
  }

  const lt = result.longTasks ?? []
  const worst = lt.reduce((m, t) => (t.maxMs > m.maxMs ? t : m), { step: '-', maxMs: 0 })
  row('longest main-thread task', `≤ ${budgets.longTaskMs} ms`, `${worst.maxMs} ms (${worst.step})`, worst.maxMs <= budgets.longTaskMs)
  for (const s of result.unresponsive ?? []) row('page responsive', 'answers within 10 s', `unresponsive at ${s}`, false)

  const readyBudget = typeof budgets.landingReadyMs === 'number' ? budgets.landingReadyMs : budgets.landingReadyMs[mode]
  const ready = result.landingReadyMs
  const held = result.landingHeldBy ? ` — held by ${result.landingHeldBy.url} (${result.landingHeldBy.ms} ms)` : ''
  row('landing ready', `≤ ${readyBudget} ms`, ready == null ? 'never ready' : `${ready} ms${held}`, ready != null && ready <= readyBudget)

  for (const w of budgets.overflowWidths) {
    const o = (result.overflow ?? []).filter((x) => x.width === w)
    const bad = o.filter((x) => x.docWidth > x.width + 1)
    row(`no horizontal overflow at ${w}`, 'page width = viewport', o.length === 0 ? 'not measured' : bad.length ? bad.map((x) => `${x.where}: ${x.docWidth}`).join(', ') : `ok (${o.length} views)`, o.length > 0 && bad.length === 0)
  }

  const small = (result.surfaces ?? []).filter((s) => s.reader)
  const smallTotal = small.reduce((n, s) => n + (s.smallText ?? 0), 0)
  row(`reader text under ${budgets.textFloorPx} px`, '0', `${smallTotal} (${small.map((s) => `${s.name} ${s.smallText}`).join(', ')})`, smallTotal === 0, budgets.enforceTextFloor)

  // Engine words on reader surfaces. Seen AS A READER (the reader pass, or a
  // walk signed in as a reader or contributor) the budget is zero and
  // enforced; seen as an admin they are reported only (an admin may see the
  // engine's readout on a reader page).
  {
    const measuredAsReader = readerSurfacesMeasured(result.surfaces)
    const hits = readerEngineHits(result.surfaces)
    row(
      'engine words on reader surfaces, as a reader',
      `${budgets.readerEngineWords ?? 0}`,
      measuredAsReader === 0 ? 'not measured (no reader surface was walked as a reader)' : hits.length ? hits.map((h) => `${h.where}: ${h.words.join(' | ')}`).join('; ') : `0 (${measuredAsReader} views)`,
      measuredAsReader > 0 && hits.reduce((n, h) => n + h.words.length, 0) <= (budgets.readerEngineWords ?? 0),
      // nothing walked as a reader is reported, not failed: the walk's own
      // "the reader side walked as a reader" step fails when the pass could not run
      measuredAsReader > 0,
    )
    const adminHits = (result.surfaces ?? []).filter((x) => x.reader && !READER_ROLES.has(x.role) && (x.engineWords ?? []).length > 0)
    row('engine words on reader surfaces, as an admin', 'reported', adminHits.length ? adminHits.map((x) => `${x.name} ${x.width}: ${x.engineWords.join(' | ')}`).join('; ') : '0', adminHits.length === 0, false)
  }

  for (const c of result.consult ?? []) {
    row(`Consult box width at ${c.width}${c.where ? ` (${c.where})` : ''}`, `≥ ${budgets.consultMinWidthPx} px`, c.boxWidth == null ? 'no Consult box found' : `${c.boxWidth} px`, c.boxWidth != null && c.boxWidth >= budgets.consultMinWidthPx)
  }

  for (const c of result.consult ?? []) {
    const at = `${c.width}${c.where ? ` (${c.where})` : ''}`
    if (c.askVisible !== undefined) row(`Consult's Ask button is fully on screen at ${at}`, 'inside the window and the dock', c.askVisible == null ? 'no Ask button found' : c.askVisible ? `at ${(c.askRect ?? []).join(',')}` : `cut off at ${(c.askRect ?? []).join(',')}`, c.askVisible === true)
    if (c.transcriptFraction != null) row(`Consult's answers share of the dock at ${at}`, `≥ ${Math.round(budgets.consultTranscriptMinFraction * 100)} %`, `${Math.round(c.transcriptFraction * 100)} %`, c.transcriptFraction >= budgets.consultTranscriptMinFraction)
  }

  for (const l of result.layout ?? []) {
    const fr = l.viewport > 0 && l.emptyBandPx != null ? l.emptyBandPx / l.viewport : null
    row(`no empty band at ${l.where}`, `≤ ${Math.round(budgets.emptyBandMaxFraction * 100)} % of ${l.viewport} px`, fr == null ? 'not measured' : `${l.emptyBandPx} px (${(fr * 100).toFixed(1)} %)`, fr != null && fr <= budgets.emptyBandMaxFraction)
    const floor = bodyTextFloorAt(l.viewport, budgets.bodyTextMinPx)
    row(`body text at ${l.where}`, `median ≥ ${floor} px`, l.bodyTextPx == null ? 'not measured' : `${l.bodyTextPx} px`, l.bodyTextPx != null && l.bodyTextPx >= floor)
    if (l.chatWidthRem !== undefined) {
      const [lo, hi] = budgets.chatWidthRem
      row(`chat reading width at ${l.where}`, `${lo}–${hi} rem`, l.chatWidthRem == null ? 'no chat column' : `${l.chatWidthRem} rem (${l.chatWidthPx} px)`, l.chatWidthRem != null && l.chatWidthRem >= lo && l.chatWidthRem <= hi)
    }
  }
  for (const d of budgets.draggables) {
    const got = (result.drags ?? []).find((x) => x.what === d)
    row(`drag ${d}`, 'moves, and is kept across a reload', got ? `${got.before} → ${got.after} → reload ${got.reloaded} px` : 'not dragged', !!got && got.moved && got.kept)
  }
  for (const m of result.consultMoves ?? []) row(`Consult ${m.what}`, 'works, the page stays usable', m.note, m.ok)

  if (result.map && !result.map.absent) {
    const mb = typeof budgets.mapReadyMs === 'number' ? budgets.mapReadyMs : budgets.mapReadyMs[mode]
    const mr = result.map.readyMs
    row('map ready', `≤ ${mb} ms`, mr == null ? 'never ready' : `${mr} ms (${result.map.renderer})`, mr != null && mr <= mb)
    const marks = Math.max(result.map.marks ?? 0, result.map.marksAfterToggle ?? 0)
    row('map marks drawn', `≤ ${budgets.mapMarksCap}`, `${result.map.marks} at rest, ${result.map.marksAfterToggle ?? '-'} with events on`, marks <= budgets.mapMarksCap)
  }

  for (const f of result.mapFits ?? []) {
    row(`map fits as a frame panel: ${f.where}`, 'drawn; its chrome inside the map, clear of headers and of itself', f.ok ? `${f.size} px · layer card ${f.layers} · legend ${f.legend} · ${f.projection ?? '?'} z${f.zoom ?? '?'}` : f.issues.join('; '), f.ok)
  }

  const zeros = result.zeroBeforeRead ?? []
  row('no "0" before the count is read', `${budgets.zeroBeforeRead}`, zeros.length ? `${zeros.length}: ${zeros.slice(0, 8).map((z) => `${z.where} "${z.text}"`).join('; ')}` : '0', zeros.length <= budgets.zeroBeforeRead)

  const errs = result.errors ?? []
  row('page and console errors', `${budgets.consoleErrors}`, errs.length ? `${errs.length}: ${errs.slice(0, 6).join(' | ')}` : '0', errs.length <= budgets.consoleErrors)

  const ids = result.phoneMachineIds ?? []
  row('machine ids as titles on the phone', `${budgets.phoneMachineIds}`, ids.length ? ids.join(', ') : '0', ids.length <= budgets.phoneMachineIds)

  const L = result.landing
  if (L) {
    const pick = (b) => (typeof b === 'number' ? b : b[mode])
    const mb = (n) => `${(n / 1e6).toFixed(2)} MB`
    row('landing requests', `≤ ${pick(budgets.landingRequests)}`, `${L.requests} (${L.findingsRequests} to /findings)`, L.requests <= pick(budgets.landingRequests))
    row('landing bytes', `≤ ${mb(pick(budgets.landingBytes))}`, `${mb(L.bytes)} (/findings ${mb(L.findingsBytes)})`, L.bytes <= pick(budgets.landingBytes))
    row('largest landing response', `≤ ${mb(pick(budgets.largestResponseBytes))}`, L.largest ? `${mb(L.largest.bytes)} ${L.largest.url}` : 'none', (L.largest?.bytes ?? 0) <= pick(budgets.largestResponseBytes))
    row('duplicate landing requests', `${budgets.duplicateRequests}`, L.duplicateRequests ? `${L.duplicateRequests}: ${L.duplicates.slice(0, 6).map((d) => `${d.url} ×${d.n}`).join(', ')}` : '0', L.duplicateRequests <= budgets.duplicateRequests)
    row('fetches by hidden regions', `${budgets.hiddenRegionFetches}`, L.hidden == null ? 'not measured (the bundle keeps no fetch log)' : L.hidden.length ? `${L.hidden.length}: ${L.hidden.slice(0, 6).join(', ')}` : '0', L.hidden != null && L.hidden.length <= budgets.hiddenRegionFetches)
  }

  // ── the phone shell (M-2): per screen, per size; the JS the landing loads
  const PS = result.phoneShell
  if (PS && budgets.phoneScreens) {
    const kb = (n) => `${(n / 1000).toFixed(1)} kB`
    for (const s of PS.screens ?? []) {
      const b = budgets.phoneScreens[s.name]
      const at = `phone ${s.width} · ${s.name}`
      if (b && mode === 'fixture') {
        row(`${at}: API requests`, `≤ ${b.requests}`, `${s.requests}`, s.requests <= b.requests)
        row(`${at}: API bytes`, `≤ ${kb(b.bytes)}`, kb(s.bytes), s.bytes <= b.bytes)
      }
      row(`${at}: duplicate requests`, '0', s.duplicates ? s.dupList.map((d) => `${d.url} ×${d.n}`).join(', ') : '0', s.duplicates === 0)
      row(`${at}: longest task`, `≤ ${budgets.phoneLongTaskMs} ms`, s.longTaskMs == null ? 'not measured' : `${s.longTaskMs} ms`, s.longTaskMs != null && s.longTaskMs <= budgets.phoneLongTaskMs)
      row(`${at}: text under 12 px`, '0', s.smallTextCount == null ? 'not measured' : `${s.smallTextCount}${s.smallText?.length ? ': ' + s.smallText.slice(0, 3).join('; ') : ''}`, s.smallTextCount === 0)
      row(`${at}: controls under 44 × 44 px`, '0', s.smallTargetCount == null ? 'not measured' : `${s.smallTargetCount}${s.smallTargets?.length ? ': ' + s.smallTargets.slice(0, 3).join('; ') : ''}`, s.smallTargetCount === 0)
      row(`${at}: no sideways scroll`, 'none', s.overflow == null ? 'not measured' : s.overflow ? 'scrolls sideways' : 'none', s.overflow === false)
      row(`${at}: the bottom bar on screen`, 'yes', s.barOk == null ? 'not measured' : s.barOk ? 'yes' : 'no', s.barOk === true)
      row(`${at}: no engine words`, 'none', (s.engine ?? []).join(', ') || 'none', (s.engine ?? []).length === 0)
    }
    if (PS.js) {
      row('phone: initial JS (gzip)', `≤ ${kb(budgets.phoneInitialJsGz)}`, kb(PS.js.initialGz), PS.js.initialGz <= budgets.phoneInitialJsGz)
      row('phone: JS before any tap (gzip)', `≤ ${kb(budgets.phoneLandingJsGz)}`, `${kb(PS.js.landingGz)} in ${PS.js.landingFiles.length} files`, PS.js.landingGz <= budgets.phoneLandingJsGz)
      const map = PS.js.landingFiles.filter((f) => /maplibre/i.test(f.path))
      row('phone: no map library before the globe is asked for', '0', `${map.length}`, map.length === 0)
    }
  }

  const W = result.watch
  if (W && budgets.watchRequests) {
    const pick = (b) => (typeof b === 'number' ? b : b[mode])
    const mb = (n) => `${(n / 1e6).toFixed(2)} MB`
    row('Watch requests', `≤ ${pick(budgets.watchRequests)}`, W.requests == null ? 'not measured' : `${W.requests}`, W.requests != null && W.requests <= pick(budgets.watchRequests))
    row('Watch bytes', `≤ ${mb(pick(budgets.watchBytes))}`, W.bytes == null ? 'not measured' : `${mb(W.bytes)}${W.largest ? ` (largest ${mb(W.largest.bytes)} ${W.largest.url})` : ''}`, W.bytes != null && W.bytes <= pick(budgets.watchBytes))
    row('duplicate Watch requests', '0', W.duplicates == null ? 'not measured' : `${W.duplicates}`, W.duplicates === 0)
  }

  for (const r of result.recordSheet ?? []) {
    const pick = (b) => (typeof b === 'number' ? b : b[mode])
    if (!r.opened) continue // the step row below names it
    const st = recordSheetStats(result.requestLog, r.phase)
    const tx = recordSheetStats(result.requestLog, `${r.phase} (text)`)
    row(`record sheet ready from ${r.from}`, `≤ ${pick(budgets.recordSheetReadyMs)} ms`, `${r.readyMs} ms`, r.readyMs != null && r.readyMs <= pick(budgets.recordSheetReadyMs))
    row(`record sheet reads from ${r.from}`, `≤ ${budgets.recordSheetRequests}`, `${st.requests} (${st.routes.join(', ') || 'none'})${st.others.length ? `; other reads meanwhile: ${st.others.length}` : ''}`, st.requests <= budgets.recordSheetRequests)
    row(`record sheet bytes from ${r.from}`, `≤ ${(pick(budgets.recordSheetBytes) / 1e6).toFixed(2)} MB`, `${(st.bytes / 1e6).toFixed(3)} MB`, st.bytes <= pick(budgets.recordSheetBytes))
    row(`record sheet reads bounded from ${r.from}`, 'depth 1, ≤ 8 ids, text ≤ 20,000 chars', [...st.unbounded, ...tx.unbounded].join(', ') || 'all bounded', st.unbounded.length + tx.unbounded.length === 0)
    row(`record sheet address from ${r.from}`, '#rec= names the record', r.address ?? 'none', !!r.address)
    if (r.text) row(`"show what was received" from ${r.from}`, `answers; ≤ ${budgets.recordTextRequests} read`, `${r.text.shown ? r.text.kind : 'nothing shown'} · ${tx.requests} read`, r.text.shown && tx.requests <= budgets.recordTextRequests)
  }
  if (result.requestLog) {
    const inLanding = (result.requestLog ?? []).filter((r) => r.phase === 'landing' && recordRouteOf(r.url))
    row("the Record sheet's reads in the landing", '0', inLanding.length ? `${inLanding.length}: ${inLanding.slice(0, 3).map((r) => r.url).join(', ')}` : '0', inLanding.length === 0)
  }

  const G = result.ledger
  if (G) {
    const pick = (b) => (typeof b === 'number' ? b : b[mode])
    // THE OWNER'S RULE: no forecast score is shown until five questions have resolved.
    const under = G.resolved != null && G.floor != null && G.resolved < G.floor
    const leak = G.scoreShown || G.brierText || G.scored
    row('ledger: no score below five resolutions', 'no score, no Brier figure while resolved < 5',
      G.trackState === 'withheld' ? `${G.resolved} / ${G.floor} resolved; ${leak ? 'a score is SHOWN' : 'no score shown'}` : `track record ${G.trackState}${G.scored ? ' (scored)' : ''}`,
      !(under && leak) && !(G.trackState !== 'scored' && leak))
    // E-3: the evaluation record is served; it answers (a published run, or the
    // designed "none published"), never "not served", "unreadable" or an error.
    if (G.proofState != null) {
      row('ledger: evaluation record read', 'published or none published', `${G.proofState}${G.proofRuns?.length ? ` (${G.proofRuns.join(', ')})` : ''}`,
        ['published', 'none_published'].includes(G.proofState))
    }
    if (G.requests) {
      row('ledger requests', `≤ ${pick(budgets.ledgerRequests)}`, `${G.requests.requests}`, G.requests.requests <= pick(budgets.ledgerRequests))
      row('ledger bytes', `≤ ${(pick(budgets.ledgerBytes) / 1e6).toFixed(2)} MB`, `${(G.requests.bytes / 1e6).toFixed(2)} MB`, G.requests.bytes <= pick(budgets.ledgerBytes))
      row('duplicate ledger requests', '0', G.requests.duplicateRequests ? G.requests.duplicates.map((d) => `${d.url} ×${d.n}`).join(', ') : '0', G.requests.duplicateRequests === 0)
      }
    }

  // ── the phone (M-0a). Each is checked only when the walk measured it (an
  // older result has none of these keys), except the phone landing, which a
  // walk that reached the phone always has.
  {
    const pick = (b) => (typeof b === 'number' ? b : b[mode])
    const mb = (n) => `${(n / 1e6).toFixed(3)} MB`
    const P = result.phoneLanding
    if (P && budgets.phoneLandingRequests) {
      row('phone landing requests', `≤ ${pick(budgets.phoneLandingRequests)}`, `${P.requests} (${P.findingsRequests} to /findings)`, P.requests <= pick(budgets.phoneLandingRequests))
      row('phone landing bytes', `≤ ${mb(pick(budgets.phoneLandingBytes))}`, `${mb(P.bytes)} (/findings ${mb(P.findingsBytes)})`, P.bytes <= pick(budgets.phoneLandingBytes))
      row('largest phone landing response', `≤ ${mb(pick(budgets.phoneLargestResponseBytes))}`, P.largest ? `${mb(P.largest.bytes)} ${P.largest.url}` : 'none', (P.largest?.bytes ?? 0) <= pick(budgets.phoneLargestResponseBytes))
      row('duplicate phone landing requests', '0', P.duplicateRequests ? `${P.duplicateRequests}: ${P.duplicates.slice(0, 6).map((d) => `${d.url} ×${d.n}`).join(', ')}` : '0', P.duplicateRequests === 0)
    }
    const O = result.phoneOpen
    if (O && budgets.phoneOpenRequests != null && O.requests > 0) {
      row('phone: opening one read', `≤ ${budgets.phoneOpenRequests} requests, ≤ ${mb(pick(budgets.phoneOpenBytes))}`, `${O.requests} requests, ${mb(O.bytes)}`, O.requests <= budgets.phoneOpenRequests && O.bytes <= pick(budgets.phoneOpenBytes))
    }
    for (const c of result.phoneConsult ?? []) {
      const k = c.composer
      row(`phone ${c.width}: question box share of the composer row`, `≥ ${Math.round(budgets.phoneComposerMinFraction * 100)} %`, k ? `${Math.round(k.fraction * 100)} % (${k.box} of ${k.row} px, ${k.controls} control${k.controls === 1 ? '' : 's'})` : 'no composer found', !!k && k.fraction >= budgets.phoneComposerMinFraction)
      row(`phone ${c.width}: Ask reachable`, 'on screen, ≥ 44 px', k ? (k.sendVisible ? 'yes' : 'no') : 'no composer found', !!k && k.sendVisible)
      if (c.answer !== undefined && c.answer !== null) {
        const a = c.answer
        row(`phone ${c.width}: answer renders markdown`, 'no literal ** or pipe-table text; a table element', [a.rawBold ? 'literal **' : null, a.pipeTable ? 'pipe-table text' : null, a.table ? null : 'no table'].filter(Boolean).join(', ') || 'ok', !a.rawBold && !a.pipeTable && a.table)
        const bad = (a.chips ?? []).filter((x) => !x.ok || x.tag !== 'BUTTON')
        row(`phone ${c.width}: answer citations are buttons with a ${budgets.phoneCitationTargetPx} px target`, `${(a.chips ?? []).length} ≥ 1, all reachable`, (a.chips ?? []).length === 0 ? 'no citation chip' : bad.length ? `${bad.length} of ${a.chips.length} fail: ${bad.slice(0, 3).map((x) => `${x.tag} ${x.box}`).join(', ')}` : `${a.chips.length} ok`, (a.chips ?? []).length > 0 && bad.length === 0)
        row(`phone ${c.width}: no raw row ids in the answer`, '0', a.rawIds ? 'raw ids printed' : '0', !a.rawIds)
      } else if (c.answer === null && c.width === 390) {
        row('phone 390: answer measured', 'measured', 'not measured', false)
      }
      // M-1 — the phone runs the desktop's turn. Checked when the walk measured it.
      if (c.turn) {
        const t = c.turn
        const s = t.sends ?? {}
        row(`phone ${c.width}: the answer streams`, 'POST 202, answer on the stream', t.streamed ? 'yes' : 'no', !!t.streamed)
        row(`phone ${c.width}: the turn contract is drawn`, 'contract block; a cost stop reads "Partial answer"', [t.contract ? null : 'no contract block', t.partial ? null : 'no partial-answer line'].filter(Boolean).join(', ') || 'ok', !!t.contract && !!t.partial)
        row(`phone ${c.width}: the request carries the desktop's fields`, 'window, grounding, on-screen; core; no writes', [s.window ? null : 'no window', s.grounding ? null : 'no grounding', s.onScreen ? null : 'no on-screen', s.model === 'core' ? null : `model ${s.model}`, s.writes ? 'allow_writes sent' : null].filter(Boolean).join(', ') || 'ok', !!s.window && !!s.grounding && !!s.onScreen && s.model === 'core' && !s.writes)
        const st = t.stop
        row(`phone ${c.width}: Stop works`, 'shown while running; stop then synthesize; the answer lands; Ask returns', st ? [st.shown ? null : 'no Stop', st.stops === 1 ? null : `${st.stops} stop calls`, st.synths === 1 ? null : `${st.synths} synthesize calls`, st.settled ? null : 'no answer after Stop', st.askBack ? null : 'Ask did not return', st.synthModel === 'core' ? null : `synthesized on ${st.synthModel}`].filter(Boolean).join(', ') || 'ok' : 'not measured', !!st && st.shown && st.stops === 1 && st.synths === 1 && st.settled && st.askBack && st.synthModel === 'core')
      }
    }
    for (const f of result.favicon ?? []) {
      row(`icon ${f.path}`, 'an image', `${f.status} ${f.type || '-'}`, f.status === 200 && /^image\//.test(f.type))
    }
  }

  for (const s of result.steps ?? []) {
    if (s.ok === false) row(`step: ${s.name}`, 'completes', s.note ?? 'failed', false)
  }
  for (const w of result.writesSent ?? []) row('GET-only', 'no write reaches the API', w, false)

  return { failures, table }
}

/** The plain-text report the walk prints. */
export function formatReport(result, evaluation) {
  const lines = []
  lines.push(`WALK mode=${result.mode} base=${result.base} at=${result.at}`)
  lines.push('')
  lines.push('BUDGETS')
  for (const r of evaluation.table) lines.push(`  [${r.verdict.padEnd(6)}] ${r.check} — ${r.value} (budget ${r.budget})`)
  lines.push('')
  lines.push('SURFACES (for the record)')
  for (const s of result.surfaces ?? []) {
    lines.push(`  ${s.name.padEnd(28)} ${String(s.width).padStart(4)} px  tiles=${s.tiles ?? '-'} groups=${s.groups ?? '-'} text=${s.textEls ?? '-'} small<${result.textFloorPx}px=${s.smallText ?? '-'} engineWords=${(s.engineWords ?? []).join('|') || '0'}`)
  }
  if (result.map) {
    lines.push('')
    lines.push('MAP')
    lines.push(`  ${JSON.stringify(result.map)}`)
  }
  if (result.flow) {
    lines.push('')
    lines.push('FLOW')
    for (const f of result.flow) lines.push(`  ${f.step}: ${JSON.stringify(f)}`)
  }
  lines.push('')
  lines.push(`REQUESTS ${result.requests?.total ?? 0} to ${result.requests?.paths ?? 0} paths; writes answered locally: ${result.requests?.refusedWrites ?? 0}; outside requests blocked: ${result.requests?.blockedOutside ?? 0}; not in fixtures: ${(result.requests?.unfixtured ?? []).length}`)
  if ((result.requests?.slow ?? []).length) lines.push('  slow (> 3 s): ' + result.requests.slow.slice(0, 12).join(', '))
  if ((result.requests?.standIns ?? []).length) lines.push(`  answered by the same route for another id: ${result.requests.standIns.length}`)
  if ((result.requests?.unfixtured ?? []).length) lines.push('  unfixtured: ' + result.requests.unfixtured.slice(0, 20).join(', '))
  lines.push('')
  lines.push(evaluation.failures.length ? `WALK_VERDICT FAIL (${evaluation.failures.length})` : 'WALK_VERDICT PASS')
  for (const f of evaluation.failures) lines.push('  - ' + f)
  return lines.join('\n')
}
