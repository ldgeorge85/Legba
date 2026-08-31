/**
 * READ TELEMETRY (D2e) — the client half of the oracle wager's instrument.
 *
 * The premise review's finding is a measurement gap: the substrate receipts
 * every WRITE and nothing records a READ, so "does the operator actually read
 * the product?" — the question the 90-day wager is graded on — has only Caddy
 * access logs behind it. An access log cannot tell a panel mount from a build
 * event and cannot see a citation chip being followed at all. This module
 * emits the acts of reading themselves.
 *
 * ── THE THREE RULES THIS MODULE EXISTS TO OBEY ────────────────────────────
 *
 * 1. TELEMETRY MUST NEVER BREAK THE UI. Every failure path here is swallowed
 *    and reported to `console.debug`, never thrown, never surfaced. A read
 *    plane that can take the workstation down with it would be turned off
 *    within a week, and then the wager would have no instrument at all. That
 *    is also why nothing here awaits: `emitRead` is fire-and-forget and
 *    returns `void`, so no call site can accidentally block a click on a
 *    network round trip.
 *
 * 2. IT MUST BE CHEAP. Events queue and drain on a {@link FLUSH_MS} timer —
 *    at most one POST every few seconds no matter how fast the operator
 *    clicks — and the queue is bounded, so a runaway emitter costs a fixed
 *    amount of memory and drops the OLDEST events (a lost old event is a
 *    smaller lie than a stalled browser).
 *
 * 3. IT MUST NOT INVENT READING. `occurred_at` is stamped at emit time, not
 *    at flush time, because the evidence is the operator's attention and that
 *    happened when they clicked. Dwell is only reported where a close/blur
 *    made it genuinely cheap to observe; we never estimate one.
 *
 * ── WHY A MODULE-LEVEL WORKSPACE, NOT A PROP ──────────────────────────────
 * The emit sites are scattered across layers that have no business knowing
 * about the stance model: a zustand store action (`selectRow`), a pure prose
 * renderer (`CitedProse`), a Dockview component factory. Threading the active
 * workspace through all of them would put telemetry plumbing into the
 * signature of half the app. Instead `App.tsx` pushes the active stance in
 * with {@link setTelemetryWorkspace} whenever it changes, and every emit
 * reads it. One writer, many readers, no prop drilling.
 */

/**
 * Spelled out rather than composed from `lib/api.ts`'s `API_BASE`, and this
 * module deliberately does NOT use `apiPost`.
 *
 * `apiPost` throws `ApiError` on any non-2xx, and rule 1 says nothing here may
 * throw; it also cannot carry `keepalive`, and `sendBeacon` needs a bare URL
 * anyway. Importing the client would additionally pull the whole REST module
 * (and its timing instrumentation) into a path that runs on every click.
 *
 * The cost is one hardcoded prefix: if `API_BASE` ever moves off `/api/v1`,
 * this constant has to move with it. `lib/readScoreboard`'s read side DOES go
 * through `apiGet`, so only the emit path carries the duplication.
 */
const ENDPOINT = '/api/v1/read-events'

/** The closed vocabulary — mirrors migration 0189's CHECK constraint. */
export type ReadEventKind =
  | 'panel_open'
  | 'workspace_open'
  | 'finding_open'
  | 'lineage_walk'
  | 'citation_drill'
  | 'consult_open'
  | 'brief_read'

export interface ReadEvent {
  occurred_at: string
  event_kind: ReadEventKind
  workspace: string
  session_nonce: string
  subject_kind?: string | null
  subject_id?: string | null
  dwell_ms?: number | null
}

/** One POST per few seconds, maximum. */
export const FLUSH_MS = 4000

/**
 * A bounded queue. Reading generates events in bursts (a workspace switch
 * seeds seven panels at once), so the ceiling is comfortably above a burst
 * while still guaranteeing the tab cannot accumulate unbounded memory if the
 * server is down for an afternoon.
 */
export const MAX_QUEUE = 200

/**
 * Duplicate-suppression window.
 *
 * React's StrictMode double-invokes effects in development, and Dockview
 * remounts a panel component on some layout operations. Either would double
 * every `panel_open` — inflating the exact number the wager is graded on, in
 * the flattering direction, which is the one bias this instrument must not
 * have. Two identical (kind, subject) events inside this window count once.
 * The window is deliberately short: a genuine re-open a second later is a
 * real act of reading and is kept.
 */
export const DEDUPE_MS = 1200

const SESSION_KEY = 'legba_read_session'

let queue: ReadEvent[] = []
let timer: ReturnType<typeof setTimeout> | null = null
let workspace = 'unknown'
let lastSeen = new Map<string, number>()
let enabled = true

/** Test seam — drop all queued state between cases. */
export function __resetReadTelemetry(): void {
  if (timer !== null) clearTimeout(timer)
  timer = null
  queue = []
  lastSeen = new Map()
  workspace = 'unknown'
  enabled = true
}

/**
 * Turn emission off for this tab.
 *
 * Not exposed in the UI: this exists so a future operator-facing "don't
 * measure me" switch has a single, honest place to land, rather than being
 * bolted on as a scattered set of `if` guards later.
 */
export function setReadTelemetryEnabled(next: boolean): void {
  enabled = next
}

/** The active stance, pushed in by `App.tsx`. See the module header. */
export function setTelemetryWorkspace(next: string): void {
  workspace = next
}

export function currentTelemetryWorkspace(): string {
  return workspace
}

/**
 * A per-browser-session nonce — enough to tell one long morning from eight
 * separate visits, which is the only cardinality the wager needs. It lives in
 * `sessionStorage` so a reload inside the same tab stays one session while a
 * new tab starts a new one. It is NOT an identity: single-operator,
 * single-tenant, and the server stores no principal alongside it.
 */
export function sessionNonce(): string {
  try {
    const existing = sessionStorage.getItem(SESSION_KEY)
    if (existing) return existing
    const minted =
      typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
        ? crypto.randomUUID()
        : `s-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
    sessionStorage.setItem(SESSION_KEY, minted)
    return minted
  } catch {
    // Private-mode / storage-disabled: a per-load nonce is still better than
    // refusing to measure. Sessions get over-counted, which is the honest
    // direction to be wrong in (it under-states how long a visit lasted).
    return `ephemeral-${Math.random().toString(36).slice(2, 10)}`
  }
}

/**
 * The dedupe identity of an event.
 *
 * The WORKSPACE is part of it, and that is not cosmetic. Without it the
 * `workspace_open` fired at boot and the one fired by an Alt+N switch a moment
 * later share a key (neither carries a subject) and the switch is silently
 * swallowed, so the stance-switch metric would have read near-zero forever.
 * Two emits are "the same read" only when the kind, the stance AND the subject
 * all match.
 */
function dedupeKey(
  kind: ReadEventKind,
  ws: string,
  subjectKind?: string | null,
  subjectId?: string | null,
): string {
  return [kind, ws, subjectKind ?? '', subjectId ?? ''].join(' | ')
}

export interface EmitOptions {
  subjectKind?: string | null
  subjectId?: string | null
  dwellMs?: number | null
  /** Override the active stance (the switch itself reports its destination). */
  workspace?: string
}

/**
 * Record one act of reading. Fire-and-forget; never throws.
 *
 * A half-subject (a kind with no id, or an id with no kind) is normalized to
 * no subject rather than emitted, because the server's 0189 CHECK would
 * reject it and one malformed call site should not cost the events batched
 * beside it.
 */
export function emitRead(kind: ReadEventKind, opts: EmitOptions = {}): void {
  if (!enabled) return
  try {
    const hasSubject =
      opts.subjectKind != null &&
      opts.subjectKind !== '' &&
      opts.subjectId != null &&
      opts.subjectId !== ''
    const subjectKind = hasSubject ? String(opts.subjectKind) : null
    const subjectId = hasSubject ? String(opts.subjectId) : null

    const ws = opts.workspace ?? workspace

    const now = Date.now()
    const key = dedupeKey(kind, ws, subjectKind, subjectId)
    const seen = lastSeen.get(key)
    if (seen !== undefined && now - seen < DEDUPE_MS) return
    lastSeen.set(key, now)

    queue.push({
      occurred_at: new Date(now).toISOString(),
      event_kind: kind,
      workspace: ws,
      session_nonce: sessionNonce(),
      subject_kind: subjectKind,
      subject_id: subjectId,
      dwell_ms: opts.dwellMs ?? null,
    })

    // Drop the OLDEST on overflow: the newest events are the ones a reader is
    // actually generating right now, and losing the tail of a stalled backlog
    // is preferable to losing live evidence.
    if (queue.length > MAX_QUEUE) queue = queue.slice(queue.length - MAX_QUEUE)

    schedule()
  } catch (err) {
    console.debug('[read-telemetry] emit failed', err)
  }
}

function schedule(): void {
  if (timer !== null) return
  timer = setTimeout(() => {
    timer = null
    void flushReadTelemetry()
  }, FLUSH_MS)
}

function authHeaders(): Record<string, string> {
  try {
    const token = localStorage.getItem('legba_token')
    return token ? { Authorization: `Bearer ${token}` } : {}
  } catch {
    return {}
  }
}

/** POST one already-detached batch. Never throws. */
async function postBatch(batch: readonly ReadEvent[]): Promise<void> {
  try {
    const res = await fetch(ENDPOINT, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'application/json',
        ...authHeaders(),
      },
      body: JSON.stringify({ events: batch }),
      keepalive: true,
    })
    if (!res.ok) {
      console.debug('[read-telemetry] server rejected batch', res.status)
    }
  } catch (err) {
    // FAIL SILENT — rule 1. The operator never learns their telemetry is down
    // from a toast, and no panel ever fails to render because of it.
    console.debug('[read-telemetry] flush failed', err)
  }
}

/**
 * Drain the queue.
 *
 * The queue is taken BEFORE the await so a flush racing new emits cannot
 * double-send, and events are NOT re-queued on failure: a read receipt is
 * worth exactly one attempt. Retrying would build an ever-growing backlog
 * that, when it finally drained, would stamp a week of reading into whatever
 * minute the network came back — a worse lie than the missing rows.
 */
export async function flushReadTelemetry(): Promise<void> {
  if (queue.length === 0) return
  const batch = queue
  queue = []
  await postBatch(batch)
}

/**
 * The unload path. `fetch` — even with `keepalive` — is not reliably
 * delivered from a closing document, so the last batch goes via
 * `sendBeacon`, which the browser owns and completes after the tab is gone.
 *
 * The beacon carries no Authorization header (`sendBeacon` cannot set one),
 * which is correct for the deployed same-origin reverse proxy and degrades to
 * a dropped final batch rather than an error when a bearer token is required.
 */
export function beaconReadTelemetry(): void {
  if (queue.length === 0) return
  const batch = queue
  queue = []
  try {
    const body = new Blob([JSON.stringify({ events: batch })], {
      type: 'application/json',
    })
    if (typeof navigator !== 'undefined' && typeof navigator.sendBeacon === 'function') {
      navigator.sendBeacon(ENDPOINT, body)
      return
    }
    // No beacon support: POST the batch we already detached. Calling
    // `flushReadTelemetry` here would send NOTHING — the queue is empty by
    // this line — which is how a browser without `sendBeacon` would have
    // silently lost every final batch of every session.
    void postBatch(batch)
  } catch (err) {
    console.debug('[read-telemetry] beacon failed', err)
  }
}

/**
 * Install the page-lifecycle hooks. Idempotent; returns a teardown.
 *
 * `visibilitychange` (hidden) is the one that actually fires on mobile and on
 * tab-close in modern browsers; `pagehide` covers bfcache navigation and
 * `beforeunload` the desktop reload. All three funnel to the same beacon, and
 * a beacon on an empty queue is a no-op, so the overlap is free.
 */
export function installReadTelemetryLifecycle(): () => void {
  if (typeof window === 'undefined') return () => {}
  const onHide = () => {
    if (document.visibilityState === 'hidden') beaconReadTelemetry()
  }
  const onLeave = () => beaconReadTelemetry()
  document.addEventListener('visibilitychange', onHide)
  window.addEventListener('pagehide', onLeave)
  window.addEventListener('beforeunload', onLeave)
  return () => {
    document.removeEventListener('visibilitychange', onHide)
    window.removeEventListener('pagehide', onLeave)
    window.removeEventListener('beforeunload', onLeave)
  }
}

/** Test seam — the events waiting to be sent. */
export function __pendingReadEvents(): readonly ReadEvent[] {
  return queue
}
