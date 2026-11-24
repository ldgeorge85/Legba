/**
 * casePlayer — the case PLAYER, as module state (C-1b/ui; note §4).
 *
 * A case drives the wall, and the wall's own `arrange` can unmount the Consult
 * tile that started it (a preset calls `api.clear()`; a stance switch restores
 * another arrangement). So nothing the case needs may live in that tile:
 *
 *   * **the player owns the case turn's EventSource.** A plain turn keeps its
 *     tile-owned stream; a case turn's stream is opened here, survives the tile
 *     unmounting, and is closed on seal, stop or the terminal frame;
 *   * **the executor runs here**, one admitted action at a time, in `seq`
 *     order, against the shell port (`lib/caseRunner`);
 *   * **the snapshot** taken before step 1 lives here, so Restore works from a
 *     re-opened tile;
 *   * **the STOP latch** lives here: pressing STOP flips the state at once —
 *     nothing with a later `seq` runs, later frames render "not executed —
 *     stopped" — and only then is the server told, with
 *     `{executed_through_seq: n}`;
 *   * **Replay** re-runs a persisted record through the same executor with no
 *     consult POST and no tool call.
 *
 * The pure reducers are in `lib/caseModel.ts`; this store sequences them and
 * performs the side effects.
 */
import { create } from 'zustand'
import { stopConsultRun } from '@/lib/api'
import {
  advanceReplay,
  caseFrameOf,
  latchStop,
  markStep,
  newLivePlayer,
  newReplayPlayer,
  nextReplayAction,
  parseCaseRecord,
  receiveLiveAction,
  receiveRefusal,
  reconcileWithRecord,
  type CaseAction,
  type CaseRecord,
  type PlayerState,
  type WorkspaceSnapshot,
} from '@/lib/caseModel'
import {
  applyAction,
  getCaseShell,
  restoreSnapshot,
  takeSnapshot,
  type CaseSideEffects,
} from '@/lib/caseRunner'

/** How long autoplay rests on each replayed step. */
export const REPLAY_STEP_MS = 1200

/** What the tile hands the player for the frames that are not case frames. */
export interface CaseStreamHandlers {
  /** An ordinary step frame (the trace / spend meter). */
  onStep: (frame: Record<string, unknown>) => void
  /** The registry's answer-bearing terminal frame. */
  onTerminal: (frame: Record<string, unknown>) => void
  /** The actor's bare final — the loop is done, the registry is persisting. */
  onActorFinal?: () => void
  /** The relay's named keepalive, already parsed by the caller. */
  onKeepalive?: (data: string) => void
  /** Any frame at all (the liveness clock). */
  onFrame?: () => void
  /** The stream died — the caller falls back to its run-status poll. */
  onError?: () => void
}

interface CasePlayerState {
  player: PlayerState | null
  /** The Consult store slice that presented the case. */
  panelId: string | null
  snapshot: WorkspaceSnapshot | null
  /** Bumped by `map reset`; the map renderers fly home on a change. */
  cameraResetNonce: number
  /**
   * Non-null while a case drives the clock. The validity Timeline follows the
   * shared window while this is set (and centres on `atMs` when one was given),
   * because outside a case its window is its own component state.
   */
  clockDrive: { atMs: number | null } | null
  /** Replay autoplay. */
  autoplay: boolean
  /** The last stop route error, shown beside the rail — the latch held anyway. */
  stopError: string | null

  beginLive: (requestId: string, panelId: string) => void
  /** Open (and own) the case turn's step stream. */
  openStream: (url: string, handlers: CaseStreamHandlers) => void
  closeStream: () => void
  /** Feed one step frame in; returns true when it was a case frame. */
  ingest: (frame: Record<string, unknown>) => boolean
  /** The terminal record landed (frame, POST 200, run poll, reload). */
  landRecord: (rawCase: unknown) => void
  /** STOP: latch now, then tell the server. */
  stop: () => Promise<void>
  /** Return the wall to the snapshot taken before step 1. */
  restore: () => void
  /** Replay a persisted record without the model. */
  replay: (rec: CaseRecord, panelId: string) => void
  /** Run the next replay step. Returns false when there is none. */
  stepReplay: () => boolean
  setAutoplay: (on: boolean) => void
  /** Drop a turn that never became a case (the POST failed). */
  abandon: (requestId: string) => void
  /** Close the rail (the wall stays as the case left it). */
  dismiss: () => void
}

let es: EventSource | null = null
let autoplayTimer: ReturnType<typeof setTimeout> | null = null

function clearAutoplay(): void {
  if (autoplayTimer !== null) {
    clearTimeout(autoplayTimer)
    autoplayTimer = null
  }
}

export const useCasePlayer = create<CasePlayerState>((set, get) => {
  const effects: CaseSideEffects = {
    resetCamera: () => set((s) => ({ cameraResetNonce: s.cameraResetNonce + 1 })),
    setTimelineAt: (atMs) => set({ clockDrive: { atMs } }),
  }

  /** Run one action, snapshotting the wall first if this is the first. */
  const execute = (a: CaseAction): void => {
    const st = get()
    if (!st.player || st.player.status === 'stopped') return
    if (!st.snapshot) set({ snapshot: takeSnapshot(getCaseShell()) })
    const out = applyAction(a, getCaseShell(), { driver: 'case', effects })
    const cur = get().player
    if (!cur) return
    let next = markStep(cur, a.seq, out.state, out.state === 'executed' ? null : out.reason)
    if (a.action === 'done' && out.state === 'executed') {
      next = {
        ...next,
        sealed: true,
        summary: typeof a.args.summary === 'string' ? a.args.summary : next.summary,
      }
    }
    set({ player: next })
  }

  const scheduleAutoplay = () => {
    clearAutoplay()
    if (!get().autoplay) return
    autoplayTimer = setTimeout(() => {
      autoplayTimer = null
      if (get().stepReplay()) scheduleAutoplay()
      else set({ autoplay: false })
    }, REPLAY_STEP_MS)
  }

  return {
    player: null,
    panelId: null,
    snapshot: null,
    cameraResetNonce: 0,
    clockDrive: null,
    autoplay: false,
    stopError: null,

    beginLive: (requestId, panelId) => {
      clearAutoplay()
      get().closeStream()
      set({
        player: newLivePlayer(requestId),
        panelId,
        snapshot: null,
        clockDrive: null,
        autoplay: false,
        stopError: null,
      })
    },

    openStream: (url, handlers) => {
      get().closeStream()
      const requestId = get().player?.requestId ?? null
      try {
        const source = new EventSource(url)
        es = source
        // Guarded like the keepalive fix: a client with no addEventListener
        // still gets its step frames.
        if (typeof source.addEventListener === 'function') {
          source.addEventListener('keepalive', (e: Event) => {
            handlers.onKeepalive?.(String((e as MessageEvent).data ?? ''))
          })
        }
        source.onmessage = (e: MessageEvent) => {
          let frame: Record<string, unknown> | null = null
          try {
            frame = JSON.parse(e.data) as Record<string, unknown>
          } catch {
            return
          }
          if (!frame || typeof frame !== 'object') return
          // A stream for a superseded case says nothing about this one.
          if (get().player?.requestId !== requestId) return
          handlers.onFrame?.()
          if (frame.type === 'final') {
            if (frame.final_source === 'registry') {
              const resp = frame.response as Record<string, unknown> | undefined
              if (resp && 'case' in resp) get().landRecord(resp.case)
              handlers.onTerminal(frame)
              get().closeStream()
              return
            }
            handlers.onActorFinal?.()
            return
          }
          if (frame.type === 'step') {
            // Case frames also land in the trace — the live view and the trace
            // stay one source of truth (note §4).
            get().ingest(frame)
            handlers.onStep(frame)
          }
        }
        source.onerror = () => {
          get().closeStream()
          handlers.onError?.()
        }
      } catch {
        es = null
      }
    },

    closeStream: () => {
      if (es) {
        es.close()
        es = null
      }
    },

    ingest: (frame) => {
      const cf = caseFrameOf(frame)
      if (!cf) return false
      const p = get().player
      if (!p || p.mode !== 'live') return true
      switch (cf.kind) {
        case 'case_action': {
          if (!cf.action) {
            set({
              player: {
                ...p,
                steps: [
                  ...p.steps,
                  {
                    seq: null,
                    action: null,
                    state: 'unreadable',
                    reason: 'this step frame could not be read',
                    label: 'unreadable step',
                    restsOn: 'no read',
                    text: null,
                  },
                ],
              },
            })
            return true
          }
          const { next, decision } = receiveLiveAction(p, cf.action)
          set({ player: next })
          if (decision === 'execute') execute(cf.action)
          return true
        }
        case 'case_refused':
          set({ player: receiveRefusal(p, cf) })
          return true
        case 'case_sealed':
          set({ player: { ...p, sealed: true, summary: cf.summary ?? p.summary } })
          return true
        case 'case_stopped':
          // The server acknowledged the stop. The latch already held; this
          // only names the step it stopped after.
          set({
            player: {
              ...latchStop(p),
              note: `stopped after step ${cf.stoppedAfterSeq ?? p.executedThrough}`,
            },
          })
          return true
      }
    },

    landRecord: (rawCase) => {
      const p = get().player
      if (!p || p.mode !== 'live') return
      const rec = parseCaseRecord(rawCase)
      if (!rec) {
        // The turn landed with no case on it ("this turn presented no case").
        set({ player: { ...p, status: p.status === 'stopped' ? 'stopped' : 'done' } })
        return
      }
      const { next, toRun } = reconcileWithRecord(p, rec)
      if (next.status === 'stopped') {
        // The latch outranks the record: a stopped case runs nothing more.
        set({ player: next })
        get().closeStream()
        return
      }
      // 'live' while the resumed steps run (the executor refuses only a
      // stopped player), then 'done'.
      set({ player: { ...next, status: 'live' } })
      // Resume from the persisted record — the authority — in seq order.
      for (const a of toRun) {
        if (get().player?.status === 'stopped') break
        execute(a)
      }
      const cur = get().player
      if (cur && cur.status !== 'stopped') set({ player: { ...cur, status: 'done' } })
      get().closeStream()
    },

    stop: async () => {
      clearAutoplay()
      const p = get().player
      if (!p || p.status === 'stopped') return
      const latched = latchStop(p)
      set({ player: latched, autoplay: false })
      if (p.mode !== 'live' || p.status === 'done') return
      try {
        await stopConsultRun(p.requestId, { executed_through_seq: latched.executedThrough })
        set({ stopError: null })
      } catch (err) {
        set({ stopError: err instanceof Error ? err.message : String(err) })
      }
    },

    restore: () => {
      clearAutoplay()
      const snap = get().snapshot
      const p = get().player
      if (p && p.status !== 'stopped' && p.status !== 'done') {
        // Restoring mid-case would let the next frame move the wall again.
        set({ player: latchStop(p) })
      }
      if (snap) restoreSnapshot(snap, getCaseShell())
      set({ snapshot: null, clockDrive: null, autoplay: false })
    },

    replay: (rec, panelId) => {
      clearAutoplay()
      get().closeStream()
      set({
        player: newReplayPlayer(rec),
        panelId,
        snapshot: null,
        clockDrive: null,
        autoplay: false,
        stopError: null,
      })
    },

    stepReplay: () => {
      const p = get().player
      if (!p) return false
      const a = nextReplayAction(p)
      if (!a) {
        if (p.mode === 'replay' && p.status === 'replay') set({ player: { ...p, status: 'done' } })
        return false
      }
      execute(a)
      const cur = get().player
      if (!cur) return false
      const next = advanceReplay(cur)
      set({ player: next })
      return next.status === 'replay'
    },

    setAutoplay: (on) => {
      set({ autoplay: on })
      if (on) scheduleAutoplay()
      else clearAutoplay()
    },

    abandon: (requestId) => {
      if (get().player?.requestId !== requestId) return
      get().closeStream()
      set({ player: null, panelId: null, snapshot: null, clockDrive: null })
    },

    dismiss: () => {
      clearAutoplay()
      get().closeStream()
      set({ player: null, panelId: null, snapshot: null, clockDrive: null, autoplay: false })
    },
  }
})

/** Non-React handle. */
export function casePlayer(): CasePlayerState {
  return useCasePlayer.getState()
}

/** Is a case currently moving the wall (live or replay, not yet finished)? */
export function isCasePlaying(p: PlayerState | null): boolean {
  return !!p && (p.status === 'live' || p.status === 'replay' || p.status === 'paused_gap')
}

/** Test/teardown helper. */
export function resetCasePlayer(): void {
  clearAutoplay()
  if (es) {
    es.close()
    es = null
  }
  useCasePlayer.setState({
    player: null,
    panelId: null,
    snapshot: null,
    cameraResetNonce: 0,
    clockDrive: null,
    autoplay: false,
    stopError: null,
  })
}
