/**
 * queryKeys — ONE react-query key per logical resource that more than one
 * surface reads (U-5). Two panels naming the same route under two keys used
 * to keep two cache entries and two polls; under one key they share both.
 * The request layer (`lib/queryCache.ts`) still collapses identical requests
 * made under different keys, but a shared key also shares the answer's
 * freshness, so a hidden panel shown later reads the frame's copy at once.
 */

/** `GET /v3/system/source-firing` — the health line, the Engine's status, the Wall. */
export const SOURCE_FIRING_KEY = ['system-status', 'source-firing'] as const
/** `GET /v3/system/analyst-cadence` — the Engine's status, the Wall. */
export const ANALYST_CADENCE_KEY = ['system-status', 'analyst-cadence'] as const

/**
 * Stale time for slow-changing operational reads (source firing, cadence):
 * the routes move on the scheduler's minutes, so a minute-old answer is the
 * same answer, and a panel shown within it does not ask again.
 */
export const SLOW_STALE_MS = 60_000

/**
 * `GET /v3/map/subject_reads?analyst_id=` — a producer's subject reads at
 * verdict weight (U-4): the globe's blanks, the Wall, the rail, the palette.
 */
export const subjectReadsKey = (analystId: string) => ['subject-reads', analystId] as const

/** `GET /v3/events?limit=500` — the globe's events layer and the flat map. */
export const WORLD_EVENTS_KEY = ['world-events'] as const
/**
 * The one events read the map's events layer and the time band's events lane
 * share (one GET answers both): the newest 500 at the LIGHT weight, asked by
 * name (roll 87: the whole row was 0.42 MB live, the Watch page's largest
 * read, 2.01 MB against its 2 MB budget). `registry/event_list_weight.py`
 * `EventSummaryRow` keeps every field either reader draws.
 */
export const WORLD_EVENTS_PATH = '/v3/events?limit=500&fields=summary'
/** `GET /situations?limit=500` — the globe's situations and the flat map. */
export const WORLD_SITUATIONS_KEY = ['world-situations'] as const
/** `GET /registry/descriptors?family=target&fields=summary` — the subject roster (names, declared scope, geometry). */
export const SUBJECT_ROSTER_KEY = ['registry-targets', 'summary'] as const
