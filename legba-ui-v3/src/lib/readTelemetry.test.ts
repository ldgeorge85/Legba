/**
 * READ TELEMETRY (D2e) — the emitter's contract.
 *
 * These hold the three rules the module exists to obey, because each of them
 * is a rule that only fails in production:
 *
 *   1. IT NEVER BREAKS THE UI. A rejecting server, a throwing `fetch`, a
 *      storage-disabled browser — none of them may propagate. If any of these
 *      tests goes red, a citation click can crash the workstation.
 *   2. IT IS CHEAP. One POST per flush window no matter how fast the clicks
 *      come, and a bounded queue.
 *   3. IT DOES NOT INVENT READING. `occurred_at` is stamped at emit, not at
 *      flush; a failed batch is dropped rather than re-queued (a retry would
 *      stamp a week of reading into the minute the network came back); and a
 *      double mount does not become two reads.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import {
  DEDUPE_MS,
  FLUSH_MS,
  MAX_QUEUE,
  __pendingReadEvents,
  __resetReadTelemetry,
  beaconReadTelemetry,
  emitRead,
  flushReadTelemetry,
  installReadTelemetryLifecycle,
  sessionNonce,
  setReadTelemetryEnabled,
  setTelemetryWorkspace,
} from './readTelemetry'

function okFetch() {
  return vi.fn().mockResolvedValue({ ok: true, status: 202 } as Response)
}

function bodyOf(mock: ReturnType<typeof okFetch>, call = 0) {
  return JSON.parse(mock.mock.calls[call][1].body as string)
}

beforeEach(() => {
  vi.useFakeTimers()
  __resetReadTelemetry()
  sessionStorage.clear()
  localStorage.clear()
  vi.stubGlobal('fetch', okFetch())
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('batching', () => {
  it('sends ONE post for a burst of events, after the flush window', async () => {
    setTelemetryWorkspace('morning_read')
    emitRead('brief_read')
    emitRead('panel_open', { subjectKind: 'panel', subjectId: 'system.wall' })
    emitRead('panel_open', { subjectKind: 'panel', subjectId: 'v4.kpi' })

    // Nothing has left the browser yet — that is the debounce doing its job.
    expect(fetch).not.toHaveBeenCalled()
    expect(__pendingReadEvents()).toHaveLength(3)

    await vi.advanceTimersByTimeAsync(FLUSH_MS + 1)

    expect(fetch).toHaveBeenCalledTimes(1)
    const [url, init] = (fetch as ReturnType<typeof okFetch>).mock.calls[0]
    expect(url).toBe('/api/v1/read-events')
    expect(init.method).toBe('POST')
    const sent = JSON.parse(init.body as string)
    expect(sent.events).toHaveLength(3)
    expect(sent.events.map((e: { event_kind: string }) => e.event_kind)).toEqual([
      'brief_read',
      'panel_open',
      'panel_open',
    ])
    expect(__pendingReadEvents()).toHaveLength(0)
  })

  it('stamps occurred_at at EMIT time, not at flush time', async () => {
    const t0 = new Date('2026-08-29T07:00:00.000Z')
    vi.setSystemTime(t0)
    emitRead('brief_read')

    // Twenty seconds pass before the batch drains.
    await vi.advanceTimersByTimeAsync(20_000)

    const sent = bodyOf(fetch as ReturnType<typeof okFetch>)
    expect(sent.events[0].occurred_at).toBe(t0.toISOString())
  })

  it('tags every event with the active workspace', async () => {
    setTelemetryWorkspace('trust')
    emitRead('panel_open', { subjectKind: 'panel', subjectId: 'system.judge_stats' })
    await vi.advanceTimersByTimeAsync(FLUSH_MS + 1)
    expect(bodyOf(fetch as ReturnType<typeof okFetch>).events[0].workspace).toBe('trust')
  })

  it('shares one session nonce across a session and persists it across reloads', async () => {
    const first = sessionNonce()
    expect(sessionNonce()).toBe(first)
    emitRead('brief_read')
    emitRead('consult_open')
    await vi.advanceTimersByTimeAsync(FLUSH_MS + 1)
    const sent = bodyOf(fetch as ReturnType<typeof okFetch>)
    expect(sent.events[0].session_nonce).toBe(first)
    expect(sent.events[1].session_nonce).toBe(first)
    // A reload keeps sessionStorage, so the morning stays one session.
    __resetReadTelemetry()
    expect(sessionNonce()).toBe(first)
  })
})

describe('honesty', () => {
  it('counts a double mount ONCE inside the dedupe window', async () => {
    // React StrictMode double-invokes effects; Dockview remounts on some
    // layout ops. Either would inflate panel_open in the flattering direction.
    emitRead('panel_open', { subjectKind: 'panel', subjectId: 'system.wall' })
    emitRead('panel_open', { subjectKind: 'panel', subjectId: 'system.wall' })
    expect(__pendingReadEvents()).toHaveLength(1)
  })

  it('counts a genuine re-open after the window', async () => {
    emitRead('panel_open', { subjectKind: 'panel', subjectId: 'system.wall' })
    vi.setSystemTime(Date.now() + DEDUPE_MS + 10)
    emitRead('panel_open', { subjectKind: 'panel', subjectId: 'system.wall' })
    expect(__pendingReadEvents()).toHaveLength(2)
  })

  it('does not collapse the same subjectless kind across two workspaces', () => {
    // The bug this pins: with the workspace out of the dedupe key, the
    // `workspace_open` fired at boot and the one fired by an Alt+N switch a
    // moment later share a key (neither has a subject) and the switch is
    // swallowed — the stance-switch metric would read near-zero forever.
    setTelemetryWorkspace('morning_read')
    emitRead('workspace_open', { workspace: 'morning_read' })
    emitRead('workspace_open', { workspace: 'desk' })
    expect(__pendingReadEvents().map((e) => e.workspace)).toEqual([
      'morning_read',
      'desk',
    ])
  })

  it('still collapses a repeat within one workspace', () => {
    emitRead('workspace_open', { workspace: 'desk' })
    emitRead('workspace_open', { workspace: 'desk' })
    expect(__pendingReadEvents()).toHaveLength(1)
  })

  it('does not collapse two DIFFERENT subjects', () => {
    emitRead('panel_open', { subjectKind: 'panel', subjectId: 'a' })
    emitRead('panel_open', { subjectKind: 'panel', subjectId: 'b' })
    expect(__pendingReadEvents()).toHaveLength(2)
  })

  it('normalizes half a subject to no subject, never emitting one', () => {
    emitRead('finding_open', { subjectKind: 'finding' })
    emitRead('citation_drill', { subjectId: 'sig-1' })
    const pending = __pendingReadEvents()
    expect(pending).toHaveLength(2)
    for (const e of pending) {
      expect(e.subject_kind).toBeNull()
      expect(e.subject_id).toBeNull()
    }
  })

  it('never re-queues a failed batch', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')))
    emitRead('brief_read')
    await vi.advanceTimersByTimeAsync(FLUSH_MS + 1)
    // Dropped, not retried: a retry would stamp old reading onto whatever
    // minute the network came back.
    expect(__pendingReadEvents()).toHaveLength(0)
    await vi.advanceTimersByTimeAsync(FLUSH_MS * 5)
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('sends null dwell rather than a fabricated zero', async () => {
    emitRead('finding_open', { subjectKind: 'finding', subjectId: 'f-1' })
    await vi.advanceTimersByTimeAsync(FLUSH_MS + 1)
    expect(bodyOf(fetch as ReturnType<typeof okFetch>).events[0].dwell_ms).toBeNull()
  })

  it('carries a dwell when the caller genuinely observed one', async () => {
    emitRead('finding_open', {
      subjectKind: 'finding',
      subjectId: 'f-1',
      dwellMs: 4200,
    })
    await vi.advanceTimersByTimeAsync(FLUSH_MS + 1)
    expect(bodyOf(fetch as ReturnType<typeof okFetch>).events[0].dwell_ms).toBe(4200)
  })
})

describe('failing silent (rule 1)', () => {
  it('swallows a rejecting fetch', async () => {
    const debug = vi.spyOn(console, 'debug').mockImplementation(() => {})
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('network down')))
    emitRead('brief_read')
    await vi.advanceTimersByTimeAsync(FLUSH_MS + 1)
    expect(debug).toHaveBeenCalledWith(
      '[read-telemetry] flush failed',
      expect.any(Error),
    )
  })

  it('swallows a non-ok response and still clears the queue', async () => {
    const debug = vi.spyOn(console, 'debug').mockImplementation(() => {})
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 503 } as Response))
    emitRead('brief_read')
    await vi.advanceTimersByTimeAsync(FLUSH_MS + 1)
    expect(__pendingReadEvents()).toHaveLength(0)
    expect(debug).toHaveBeenCalledWith(
      '[read-telemetry] server rejected batch',
      503,
    )
  })

  it('keeps measuring when sessionStorage throws (private mode)', () => {
    const spy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('storage disabled')
    })
    expect(() => emitRead('brief_read')).not.toThrow()
    expect(__pendingReadEvents()).toHaveLength(1)
    expect(__pendingReadEvents()[0].session_nonce).toMatch(/^ephemeral-/)
    spy.mockRestore()
  })

  it('flushing an empty queue does nothing at all', async () => {
    await flushReadTelemetry()
    expect(fetch).not.toHaveBeenCalled()
  })
})

describe('bounded cost (rule 2)', () => {
  it('caps the queue and drops the OLDEST events', () => {
    for (let i = 0; i < MAX_QUEUE + 25; i++) {
      emitRead('panel_open', { subjectKind: 'panel', subjectId: `p-${i}` })
    }
    const pending = __pendingReadEvents()
    expect(pending).toHaveLength(MAX_QUEUE)
    // The newest survived; the oldest were dropped.
    expect(pending[pending.length - 1].subject_id).toBe(`p-${MAX_QUEUE + 24}`)
    expect(pending.some((e) => e.subject_id === 'p-0')).toBe(false)
  })

  it('can be switched off entirely', () => {
    setReadTelemetryEnabled(false)
    emitRead('brief_read')
    expect(__pendingReadEvents()).toHaveLength(0)
  })
})

describe('the unload path', () => {
  it('beacons the last batch on visibilitychange=hidden', () => {
    const beacon = vi.fn().mockReturnValue(true)
    vi.stubGlobal('navigator', { sendBeacon: beacon })
    const teardown = installReadTelemetryLifecycle()

    emitRead('brief_read')
    Object.defineProperty(document, 'visibilityState', {
      value: 'hidden',
      configurable: true,
    })
    document.dispatchEvent(new Event('visibilitychange'))

    expect(beacon).toHaveBeenCalledTimes(1)
    expect(beacon.mock.calls[0][0]).toBe('/api/v1/read-events')
    expect(__pendingReadEvents()).toHaveLength(0)
    teardown()
  })

  it('beacons on pagehide too, and a second beacon on an empty queue is a no-op', () => {
    const beacon = vi.fn().mockReturnValue(true)
    vi.stubGlobal('navigator', { sendBeacon: beacon })
    const teardown = installReadTelemetryLifecycle()
    emitRead('consult_open')
    window.dispatchEvent(new Event('pagehide'))
    window.dispatchEvent(new Event('pagehide'))
    expect(beacon).toHaveBeenCalledTimes(1)
    teardown()
  })

  it('falls back to fetch when sendBeacon is unavailable', async () => {
    vi.stubGlobal('navigator', {})
    emitRead('brief_read')
    beaconReadTelemetry()
    await vi.advanceTimersByTimeAsync(1)
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('teardown removes the listeners', () => {
    const beacon = vi.fn().mockReturnValue(true)
    vi.stubGlobal('navigator', { sendBeacon: beacon })
    installReadTelemetryLifecycle()()
    emitRead('brief_read')
    window.dispatchEvent(new Event('pagehide'))
    expect(beacon).not.toHaveBeenCalled()
  })
})

describe('auth', () => {
  it('carries the operator bearer token when one is stored', async () => {
    localStorage.setItem('legba_token', 'tok-123')
    emitRead('brief_read')
    await vi.advanceTimersByTimeAsync(FLUSH_MS + 1)
    const init = (fetch as ReturnType<typeof okFetch>).mock.calls[0][1]
    expect(init.headers.Authorization).toBe('Bearer tok-123')
  })

  it('sends no Authorization header when there is no token', async () => {
    emitRead('brief_read')
    await vi.advanceTimersByTimeAsync(FLUSH_MS + 1)
    const init = (fetch as ReturnType<typeof okFetch>).mock.calls[0][1]
    expect(init.headers.Authorization).toBeUndefined()
  })
})
