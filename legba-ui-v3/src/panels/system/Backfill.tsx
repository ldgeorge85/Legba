/**
 * Backfill / Catch-up Replay (`system.backfill`) — P-12 operator surface.
 *
 * A target that subscribes AFTER signals already exist (a late-joining
 * subscription, or a source auto-wired into a running target) misses the
 * historical slice it would have matched. This panel triggers the one-time
 * **predicate backfill** over the persistent signal pool for a chosen target
 * and shows the handoff result:
 *
 *   - **delivered** — how many historical signals the catch-up replayed
 *     (oldest-first, predicate-filtered, deduped across the target's bindings).
 *   - **boundary_seq** — the stream sequence captured at catch-up time:
 *     history is `seq <= boundary_seq`, the forward stream resumes at
 *     `boundary_seq + 1`. The two halves tile the sequence space exactly once
 *     (no gap, no duplicate at the handoff).
 *   - **forward_consumer** — the durable consumer (re)bound to start at
 *     `forward_start_seq`, so the live stream picks up exactly where the
 *     catch-up left off.
 *
 * Backed by the P-12 backfill seam (`Backfiller.catch_up_and_forward`,
 * src/legba/runtime/subscription/backfill.py) via `POST
 * /api/v1/registry/targets/{target_id}/backfill` (see `lib/api.triggerBackfill`).
 *
 * After a run we live-tail `legba.signals.>` to show the forward stream is
 * flowing again — the visible confirmation that the late-joiner is caught up
 * AND live.
 */

import { useMutation } from '@tanstack/react-query'
import { useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { ScopePicker } from '@/components/ScopePicker'
import { triggerBackfill, type BackfillResult } from '@/lib/api'
import { useLiveTail } from '@/lib/useLiveTail'
import type { PanelProps } from '@/types'

function fmtTime(iso: string): string {
  const t = Date.parse(iso)
  if (!Number.isFinite(t)) return iso
  return new Date(t).toLocaleString()
}

export default function BackfillPanel({ registration }: PanelProps) {
  const [targetId, setTargetId] = useState('')
  const [limit, setLimit] = useState('')
  const [result, setResult] = useState<BackfillResult | null>(null)
  // Live forward-stream tick: increments on each signal seen after a run, so the
  // operator can confirm the late-joiner is caught up AND receiving live.
  const [forwardSeen, setForwardSeen] = useState(0)

  const run = useMutation<BackfillResult, Error, void>({
    mutationFn: () => {
      const n = parseInt(limit, 10)
      return triggerBackfill(targetId, {
        limitPerBinding: Number.isFinite(n) && n > 0 ? n : undefined,
      })
    },
    onSuccess: (res) => {
      setResult(res)
      setForwardSeen(0)
    },
  })

  // Watch the forward stream only after a successful run (the live half of the
  // handoff). Inert under test — the stub WS never fires.
  const { connected } = useLiveTail(
    'legba.signals.>',
    () => setForwardSeen((n) => n + 1),
    result != null,
  )

  // NB: the run trigger is disabled in this build (backend-not-exposed; the
  // POST is an honest 501), so there is no "valid target" arming check — the
  // button is permanently inert. `limitInvalid` is still surfaced as a hint.
  const limitInvalid = limit.trim() !== '' && !(parseInt(limit, 10) > 0)

  return (
    <PanelChrome
      registration={registration}
      subtitle={
        run.isPending
          ? 'running backfill…'
          : result
            ? `delivered ${result.delivered} · forward from ${result.cursor.forward_start_seq}`
            : 'pick a late-subscribed target'
      }
    >
      <div className="flex-1 overflow-auto text-xs space-y-3" data-testid="backfill-body">
        {/* control row */}
        <section className="bg-surface-100 border border-slate-800 rounded p-2 space-y-2">
          <div className="text-slate-400 text-[10px] uppercase tracking-wide">
            one-time predicate backfill over the persistent signal pool
          </div>
          <label className="block">
            <span className="text-slate-500 text-[10px]">target (pick a registered target)</span>
            <ScopePicker
              family="target"
              value={targetId}
              onChange={(v) => {
                setTargetId(v)
                setResult(null)
              }}
              placeholder="select a target…"
              className="w-full bg-surface-100 border border-slate-800 rounded p-1 px-2 text-[11px] font-mono text-slate-200"
              testId="backfill-target"
            />
          </label>
          <label className="block">
            <span className="text-slate-500 text-[10px]">limit per binding (optional — caps the catch-up)</span>
            <input
              className="w-full bg-surface-100 border border-slate-800 rounded p-1 px-2 text-[11px] font-mono"
              value={limit}
              placeholder="e.g. 500 — blank = no cap"
              inputMode="numeric"
              onChange={(e) => setLimit(e.target.value)}
              data-testid="backfill-limit"
            />
            {limitInvalid && (
              <span className="text-rose-400 text-[10px]" data-testid="backfill-limit-error">
                limit must be a positive integer
              </span>
            )}
          </label>
          {/*
            Backend-not-exposed: the registry-side POST
            /targets/{id}/backfill is an HONEST 501 — the P-12 catch-up replay
            (`Backfiller.catch_up_and_forward`) is a runtime-plane operation,
            not reachable through the registry API in this build (cross-plane
            loopback, no Caddy route). Rather than surface a button that always
            errors, the trigger is DISABLED with a clear note. Re-enable by
            wiring the runtime POST trigger + a registry→runtime proxy (tracked
            follow-up; see FEATURE_COMPLETE_PLAN api-ui-surface item 5b).
          */}
          <button
            type="button"
            disabled
            title="The backfill trigger is not exposed through the registry API in this build (runtime-plane operation; honest 501)."
            className="bg-surface-200 disabled:opacity-50 disabled:cursor-not-allowed text-slate-400 rounded px-3 py-1 text-[11px] border border-slate-800"
            data-testid="backfill-run"
          >
            run backfill
          </button>
          <div className="text-[10px] text-amber-300/80" data-testid="backfill-disabled-note">
            backend not exposed — the catch-up replay runs on the runtime plane and
            isn&apos;t reachable through the registry API in this build (honest 501).
            Trigger it operator-side, or wire the runtime proxy (tracked follow-up).
          </div>
        </section>

        {/* progress */}
        {run.isPending && (
          <div
            className="flex items-center gap-2 text-sky-300 bg-sky-950/40 border border-sky-900 rounded px-2 py-1"
            data-testid="backfill-progress"
          >
            <span className="inline-block w-3 h-3 border-2 border-sky-400 border-t-transparent rounded-full animate-spin" />
            replaying historical slice for {targetId}…
          </div>
        )}

        {/* error */}
        {run.isError && (
          <div className="text-rose-300 bg-rose-900/20 border border-rose-800 rounded px-2 py-1" data-testid="backfill-error">
            backfill failed: {run.error.message}
          </div>
        )}

        {/* result */}
        {result && !run.isPending && (
          <section
            className="bg-surface-100 border border-emerald-900 rounded p-2 space-y-2"
            data-testid="backfill-result"
          >
            <div className="flex items-center gap-2">
              <span className="text-emerald-300 text-[11px] font-medium">
                ✓ caught up — {result.target_id}
              </span>
            </div>
            <div className="grid grid-cols-2 gap-2">
              <Stat label="delivered (historical)" value={String(result.delivered)} testId="backfill-delivered" />
              <Stat label="boundary_seq" value={String(result.cursor.boundary_seq)} testId="backfill-boundary" />
              <Stat label="forward from seq" value={String(result.cursor.forward_start_seq)} testId="backfill-forward-seq" />
              <Stat
                label="forward consumer"
                value={result.forward_consumer ?? '(pool-only — no stream)'}
                testId="backfill-consumer"
              />
              <Stat label="boundary captured" value={fmtTime(result.cursor.captured_at)} />
              <Stat
                label="stream present"
                value={result.cursor.stream_present ? 'yes' : 'no (pure pool catch-up)'}
              />
            </div>

            {/* forward-stream confirmation (live half of the handoff) */}
            <div className="text-[10px] text-slate-400 flex items-center gap-2" data-testid="backfill-forward-live">
              <span
                className={`inline-block w-1.5 h-1.5 rounded-full ${connected ? 'bg-emerald-400' : 'bg-slate-600'}`}
              />
              forward stream {connected ? 'live' : 'idle'} · {forwardSeen} signal
              {forwardSeen === 1 ? '' : 's'} since handoff
            </div>

            {/* delivered ids preview */}
            {result.delivered_ids.length > 0 && (
              <details className="text-[10px]">
                <summary className="cursor-pointer text-slate-400">
                  delivered signal ids ({result.delivered_ids.length})
                </summary>
                <div className="mt-1 max-h-40 overflow-auto font-mono text-slate-500 space-y-0.5" data-testid="backfill-ids">
                  {result.delivered_ids.slice(0, 200).map((id) => (
                    <div key={id} className="truncate">{id}</div>
                  ))}
                  {result.delivered_ids.length > 200 && (
                    <div className="text-slate-600">+{result.delivered_ids.length - 200} more…</div>
                  )}
                </div>
              </details>
            )}

            {result.delivered === 0 && (
              <div className="text-slate-500 text-[11px]" data-testid="backfill-empty">
                no historical signals matched this target's bindings — nothing to replay (the
                forward consumer is still bound at boundary+1).
              </div>
            )}
          </section>
        )}
      </div>
    </PanelChrome>
  )
}

function Stat({ label, value, testId }: { label: string; value: string; testId?: string }) {
  return (
    <div className="bg-surface-200 rounded px-2 py-1" data-testid={testId}>
      <div className="text-[9px] uppercase tracking-wide text-slate-500">{label}</div>
      <div className="text-slate-200 font-mono text-[11px] truncate" title={value}>
        {value}
      </div>
    </div>
  )
}
